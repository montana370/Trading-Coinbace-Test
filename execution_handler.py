"""
execution_handler.py — Ejecución inteligente y ciclo de vida de posiciones.

* Smart Order Chasing Maker-Only: órdenes Post-Only en el mejor bid; si no se
  llenan en `chase_interval_s` y la señal sigue viva, cancela y reposiciona.
  Aborta si el precio supera el deslizamiento máximo o rompe el RR neto /
  Fee-Hurdle con el nuevo precio.
* Gestión de posición (software, tick a tick):
    - Hard stop ATR → salida a mercado (taker) si best_bid ≤ stop.
    - +1.2R → stop a Fee-Adjusted Breakeven (PnL neto real ≥ 0).
    - Trailing stop por ATR tras armar breakeven.
    - TPs parciales escalonados como órdenes Post-Only en reposo (maker),
      anclados a zonas de liquidez del libro L2.
* Kill-Switch: cancela TODAS las órdenes abiertas del universo, liquida las
  posiciones a mercado y bloquea el bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from config import Settings
from database import AsyncDatabase
from exchange import ExchangeError, Gateway, PostOnlyRejected, new_client_order_id, round_step
from market_data import MarketState
from models import OrderStatus, OrderUpdate, Position, Regime, Side, Signal, StrategyName, TPLevel
from risk_manager import RiskManager, SizingResult, fee_adjusted_breakeven, net_reward_risk
from strategy_engine import StrategyEngine, fee_hurdle

if TYPE_CHECKING:
    from telegram_bot import TelegramNotifier

log = logging.getLogger("execution")

SignalValidator = Callable[[Signal], tuple[bool, str]]


@dataclass
class TrackedOrder:
    client_order_id: str
    product_id: str
    side: Side
    purpose: str                    # ENTRY | TAKE_PROFIT | EXIT
    price: float
    size: float
    position_id: str | None = None
    order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    created_at: float = field(default_factory=time.time)
    stop_trigger: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def row(self, mode: str) -> dict[str, Any]:
        return {
            "order_id": self.order_id or self.client_order_id,
            "client_order_id": self.client_order_id,
            "product_id": self.product_id,
            "side": self.side.value,
            "purpose": self.purpose,
            "price": self.price,
            "size": self.size,
            "filled_size": self.filled,
            "status": self.status.value,
            "mode": mode,
            "created_at": self.created_at,
            "updated_at": time.time(),
        }


@dataclass
class EntryResult:
    filled: float
    avg_price: float
    fees: float
    maker: bool
    attempts: int
    reason: str


class ExecutionHandler:
    def __init__(
        self,
        settings: Settings,
        gateway: Gateway,
        markets: dict[str, MarketState],
        strategy: StrategyEngine,
        risk: RiskManager,
        db: AsyncDatabase,
        notifier: "TelegramNotifier",
        validator: SignalValidator,
    ) -> None:
        self.s = settings
        self.gw = gateway
        self.markets = markets
        self.strategy = strategy
        self.risk = risk
        self.db = db
        self.notifier = notifier
        self.validator = validator
        self.mode = gateway.mode
        self.positions: dict[str, Position] = {}               # position_id → Position
        self.orders_by_cid: dict[str, TrackedOrder] = {}
        self.orders_by_oid: dict[str, TrackedOrder] = {}
        self._early: dict[str, OrderUpdate] = {}               # updates antes del ack REST
        self.entry_tasks: dict[str, asyncio.Task[None]] = {}   # product_id → task
        self._close_locks: dict[str, asyncio.Lock] = {}
        self.gw.on_order_update = self.on_order_update
        self.closed_trades_cache: list[dict[str, Any]] = []
        self._exchange_stop_at: dict[str, float] = {}           # position_id → stop colocado
        self._exchange_stop_ts: dict[str, float] = {}
        self._stop_update_tasks: dict[str, asyncio.Task[None]] = {}

    # ═════════════════════════════════════════════════════════════════════
    # Registro de órdenes / eventos
    # ═════════════════════════════════════════════════════════════════════
    @property
    def open_products(self) -> set[str]:
        return {p.product_id for p in self.positions.values()}

    @property
    def pending_products(self) -> set[str]:
        return {pid for pid, t in self.entry_tasks.items() if not t.done()}

    def _track(self, t: TrackedOrder) -> None:
        self.orders_by_cid[t.client_order_id] = t

    def _bind(self, t: TrackedOrder, order_id: str) -> None:
        t.order_id = order_id
        self.orders_by_oid[order_id] = t
        early = self._early.pop(order_id, None)
        if early:
            self._apply_update(t, early)
        self.db.fire(self.db.sync.upsert_order, t.row(self.mode))

    async def on_order_update(self, upd: OrderUpdate) -> None:
        """Callback único: canal `user` (LIVE) o simulador (PAPER)."""
        t = self.orders_by_oid.get(upd.order_id) or self.orders_by_cid.get(upd.client_order_id)
        if t is None:
            if upd.client_order_id.startswith(self.s.execution.order_prefix):
                if len(self._early) > 500:
                    self._early.pop(next(iter(self._early)))
                self._early[upd.order_id] = upd
            return
        if t.order_id is None:
            t.order_id = upd.order_id
            self.orders_by_oid[upd.order_id] = t
        prev_filled, prev_fees = t.filled, t.fees
        self._apply_update(t, upd)
        d_fill = t.filled - prev_filled
        d_fees = t.fees - prev_fees
        if t.purpose == "TAKE_PROFIT" and d_fill > 1e-12 and t.position_id:
            await self._on_tp_fill(t, d_fill, d_fees)
        self.db.fire(self.db.sync.upsert_order, t.row(self.mode))

    @staticmethod
    def _apply_update(t: TrackedOrder, upd: OrderUpdate) -> None:
        if upd.filled_size >= t.filled:
            t.filled = upd.filled_size
            t.avg_price = upd.avg_price or t.avg_price
            t.fees = max(t.fees, upd.total_fees)
        # nunca retroceder de un estado terminal
        if not t.status.is_terminal:
            t.status = upd.status
        t.event.set()

    async def _wait_terminal(self, t: TrackedOrder, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not t.status.is_terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            t.event.clear()
            try:
                await asyncio.wait_for(t.event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True

    async def _sync_from_rest(self, t: TrackedOrder) -> None:
        """Reconciliación vía REST (por si el WebSocket perdió un evento)."""
        if not t.order_id:
            return
        try:
            upd = await self.gw.get_order(t.order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_order %s falló: %s", t.order_id, exc)
            return
        if upd:
            await self.on_order_update(upd)

    async def _cancel_and_settle(self, t: TrackedOrder, timeout: float = 5.0) -> None:
        if t.status.is_terminal or not t.order_id:
            return
        try:
            await self.gw.cancel([t.order_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel %s falló: %s", t.order_id, exc)
        if not await self._wait_terminal(t, timeout):
            await self._sync_from_rest(t)

    # ═════════════════════════════════════════════════════════════════════
    # Entradas — Smart Order Chasing (Maker-Only)
    # ═════════════════════════════════════════════════════════════════════
    def submit_entry(self, signal: Signal, sizing: SizingResult) -> None:
        if signal.product_id in self.entry_tasks and not self.entry_tasks[signal.product_id].done():
            return
        task = asyncio.create_task(self._entry_flow(signal, sizing), name=f"entry-{signal.product_id}")
        self.entry_tasks[signal.product_id] = task

    async def _entry_flow(self, signal: Signal, sizing: SizingResult) -> None:
        try:
            result = await self._chase(signal, sizing)
            if result.filled > 0:
                await self._open_position(signal, result, sizing)
            else:
                log.info("[%s] entrada abortada: %s", signal.product_id, result.reason)
                self.db.fire(self.db.sync.log_event, "INFO", "ENTRY_ABORT",
                             f"{signal.product_id}: {result.reason}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Error en flujo de entrada %s", signal.product_id)
            self.db.fire(self.db.sync.log_event, "ERROR", "ENTRY", f"{signal.product_id}: {exc}")

    async def _chase(self, signal: Signal, sizing: SizingResult) -> EntryResult:
        ex = self.s.execution
        mk = self.markets[signal.product_id]
        meta = self.gw.meta(signal.product_id)
        max_price = signal.ref_price * (1 + ex.max_chase_slippage_pct / 100.0)
        target = sizing.base_size
        risk_budget = sizing.risk_usd
        filled = cost = fees = 0.0
        attempts = 0
        reason = "sin llenado"
        current: TrackedOrder | None = None
        try:
            while attempts < ex.max_chase_attempts:
                attempts += 1
                if not self.risk.entries_allowed:
                    reason = f"riesgo: {self.risk.status.value}"
                    break
                ok, why = self.validator(signal)
                if not ok:
                    reason = f"señal invalidada: {why}"
                    break
                bid, ask = mk.book.best_bid, mk.book.best_ask
                if bid is None or ask is None:
                    reason = "libro no disponible"
                    break
                # mejor bid; si hay ≥2 ticks de spread, mejora 1 tick (prioridad sin cruzar)
                price = bid
                if ask - bid >= 2 * meta.quote_increment - 1e-12:
                    price = bid + meta.quote_increment
                price = round_step(price, meta.quote_increment)
                if price > max_price:
                    reason = f"precio escapó {price:.6g} > máx {max_price:.6g}"
                    break
                valid_rr, rr = self.risk.check_entry_still_valid(
                    price, signal.stop_price, signal.take_profit,
                    self.strategy.maker_fee, self.strategy.taker_fee,
                )
                fh = fee_hurdle(price, signal.take_profit, mk.book.spread_pct or 0.0,
                                self.strategy.maker_fee, self.s.fees)
                if not valid_rr or not fh.ok:
                    reason = f"RR neto {rr:.2f} / {fh.reason} roto al reprecio"
                    break
                # ajustar tamaño al riesgo presupuestado con el nuevo precio
                _, risk_u, _ = net_reward_risk(price, signal.stop_price, signal.take_profit,
                                               self.strategy.maker_fee, self.strategy.taker_fee)
                size = min(target - filled, risk_budget / risk_u - filled if risk_u > 0 else 0.0)
                size = round_step(size, meta.base_increment)
                if size < meta.base_min_size or size * price < meta.quote_min_size:
                    reason = "restante bajo mínimo"
                    break
                current = TrackedOrder(
                    client_order_id=new_client_order_id(ex.order_prefix, "en"),
                    product_id=signal.product_id, side=Side.BUY, purpose="ENTRY",
                    price=price, size=size,
                )
                self._track(current)
                try:
                    oid = await self.gw.place_post_only(signal.product_id, Side.BUY, size, price,
                                                        current.client_order_id)
                except PostOnlyRejected:
                    current.status = OrderStatus.REJECTED
                    await asyncio.sleep(0.25)
                    continue
                except ExchangeError as exc:
                    current.status = OrderStatus.FAILED
                    reason = f"exchange: {exc}"
                    break
                self._bind(current, oid)
                await self._wait_terminal(current, ex.chase_interval_s)
                if not current.status.is_terminal:
                    await self._cancel_and_settle(current)
                filled += current.filled
                cost += current.filled * current.avg_price
                fees += current.fees
                if filled >= target - meta.base_increment or (
                    filled > 0 and (target - filled) * price < meta.quote_min_size
                ):
                    reason = "llenado completo"
                    break
                current = None
        except asyncio.CancelledError:
            if current is not None and not current.status.is_terminal:
                await self._cancel_and_settle(current)
                filled += current.filled
                cost += current.filled * current.avg_price
                fees += current.fees
            if filled > 0:  # no perder el inventario comprado
                return EntryResult(filled, cost / filled, fees, True, attempts, "cancelado con llenado parcial")
            raise
        avg = cost / filled if filled > 0 else 0.0
        if filled > 0 and reason not in ("llenado completo",):
            reason = f"llenado parcial ({reason})"
        return EntryResult(filled, avg, fees, True, attempts, reason)

    # ═════════════════════════════════════════════════════════════════════
    # Apertura y gestión
    # ═════════════════════════════════════════════════════════════════════
    async def _open_position(self, signal: Signal, res: EntryResult, sizing: SizingResult) -> None:
        mk = self.markets[signal.product_id]
        meta = self.gw.meta(signal.product_id)
        size = round_step(res.filled, meta.base_increment)
        pos = Position(
            position_id=uuid.uuid4().hex[:12],
            product_id=signal.product_id,
            strategy=signal.strategy,
            regime=signal.regime,
            entry_price=res.avg_price,
            size=size,
            initial_size=size,
            stop_price=signal.stop_price,
            initial_stop=signal.stop_price,
            take_profit=signal.take_profit,
            atr_at_entry=signal.atr,
            entry_fee_paid=res.fees,
            entry_fee_rate=(res.fees / (res.filled * res.avg_price)) if res.filled > 0 else self.strategy.maker_fee,
            opened_at=time.time(),
            obi_at_entry=signal.obi,
            highest_price=res.avg_price,
        )
        pos.tp_levels = self._build_tp_levels(pos, mk)
        self.positions[pos.position_id] = pos
        await self._place_tp_orders(pos)
        await self._persist_position(pos)
        self.notifier.notify_entry(pos, signal, res, sizing, self.mode)
        self.db.fire(self.db.sync.log_event, "INFO", "ENTRY",
                     f"{pos.product_id} BUY {pos.size} @ {pos.entry_price:.6g} ({signal.strategy.value})")

    def _build_tp_levels(self, pos: Position, mk: MarketState) -> list[TPLevel]:
        if pos.strategy is StrategyName.TREND:
            # Sin TP: un único bracket con límite muy lejano aloja el stop en el exchange.
            return [TPLevel(price=pos.take_profit, fraction=1.0)]
        meta = self.gw.meta(pos.product_id)
        prices = self.strategy.liquidity_tp_levels(mk, pos.entry_price, pos.r_value)
        fracs = list(self.s.strategy.partial_tp_fractions)
        levels: list[TPLevel] = []
        for px, fr in zip(prices, fracs):
            if px < pos.take_profit:
                levels.append(TPLevel(price=px, fraction=fr))
        rest = 1.0 - sum(lv.fraction for lv in levels)
        if rest > 1e-9:
            levels.append(TPLevel(price=pos.take_profit, fraction=rest))
        # fusionar tramos menores al mínimo del exchange
        merged: list[TPLevel] = []
        carry = 0.0
        for lv in levels:
            qty = pos.initial_size * (lv.fraction + carry)
            if qty < meta.base_min_size or qty * lv.price < meta.quote_min_size:
                carry += lv.fraction
                continue
            merged.append(TPLevel(price=lv.price, fraction=lv.fraction + carry))
            carry = 0.0
        if carry > 0 and merged:
            merged[-1].fraction += carry
        return merged or [TPLevel(price=pos.take_profit, fraction=1.0)]

    async def _place_tp_orders(self, pos: Position) -> None:
        meta = self.gw.meta(pos.product_id)
        remaining = pos.size
        open_levels = [lv for lv in pos.tp_levels if not lv.filled]
        for i, lv in enumerate(open_levels):
            if lv.order_id:
                continue
            last = i == len(open_levels) - 1
            qty = remaining if last else round_step(pos.initial_size * lv.fraction, meta.base_increment)
            qty = min(qty, remaining)
            qty = round_step(qty, meta.base_increment)
            if qty < meta.base_min_size:
                continue
            mk = self.markets[pos.product_id]
            px = lv.price
            bid = mk.book.best_bid
            if bid is not None and px <= bid:  # ya alcanzado: un tick sobre el bid
                px = bid + meta.quote_increment
            use_bracket = self.s.execution.exchange_stops
            trigger = self.exchange_stop_price(pos) if use_bracket else 0.0
            t = TrackedOrder(
                client_order_id=new_client_order_id(self.s.execution.order_prefix, "tp"),
                product_id=pos.product_id, side=Side.SELL, purpose="TAKE_PROFIT",
                price=px, size=qty, position_id=pos.position_id, stop_trigger=trigger,
            )
            self._track(t)
            try:
                if use_bracket:
                    oid = await self.gw.place_bracket_sell(pos.product_id, qty, px, trigger, t.client_order_id)
                else:
                    oid = await self.gw.place_post_only(pos.product_id, Side.SELL, qty, px, t.client_order_id)
            except PostOnlyRejected:
                t.status = OrderStatus.REJECTED
                continue
            except ExchangeError as exc:
                t.status = OrderStatus.FAILED
                log.warning("TP %s no colocado: %s", pos.product_id, exc)
                continue
            self._bind(t, oid)
            lv.order_id, lv.client_order_id = oid, t.client_order_id
            remaining -= qty
        if self.s.execution.exchange_stops:
            self._exchange_stop_at[pos.position_id] = self.exchange_stop_price(pos)
            self._exchange_stop_ts[pos.position_id] = time.time()

    def exchange_stop_price(self, pos: Position) -> float:
        """Stop de respaldo en el exchange: ligeramente por debajo del stop software."""
        meta = self.gw.meta(pos.product_id)
        return round_step(pos.stop_price * (1 - self.s.execution.exchange_stop_buffer_pct / 100), meta.quote_increment)

    def _schedule_exchange_stop_update(self, pos: Position) -> None:
        if not self.s.execution.exchange_stops or pos.closing:
            return
        placed = self._exchange_stop_at.get(pos.position_id, 0.0)
        target = self.exchange_stop_price(pos)
        # sólo si el stop sube de forma significativa (≥ 0.25 ATR) y con control de frecuencia
        min_step = 0.25 * (pos.atr_at_entry or 0.0)
        if target <= placed + min_step and not (pos.breakeven_armed and placed < pos.entry_price):
            return
        if time.time() - self._exchange_stop_ts.get(pos.position_id, 0.0) < self.s.execution.exchange_stop_min_update_s:
            return
        task = self._stop_update_tasks.get(pos.position_id)
        if task and not task.done():
            return
        self._stop_update_tasks[pos.position_id] = asyncio.create_task(self._replace_brackets(pos.position_id))

    async def _replace_brackets(self, position_id: str) -> None:
        """Cancela y recoloca los brackets con el stop actualizado (no son editables en Coinbase)."""
        pos = self.positions.get(position_id)
        if pos is None:
            return
        lock = self._close_locks.setdefault(position_id, asyncio.Lock())
        async with lock:
            if pos.closing or position_id not in self.positions:
                return
            self._exchange_stop_ts[position_id] = time.time()
            active = [t for t in self.orders_by_cid.values()
                      if t.position_id == position_id and t.purpose == "TAKE_PROFIT" and not t.status.is_terminal]
            await asyncio.gather(*(self._cancel_and_settle(t) for t in active))
            if pos.closing or position_id not in self.positions or pos.size <= 0:
                return
            for lv in pos.tp_levels:
                if not lv.filled:
                    lv.order_id = None
            await self._place_tp_orders(pos)
            await self._persist_position(pos)
            log.info("[%s] stop en exchange actualizado → %.6g", pos.product_id, self._exchange_stop_at.get(position_id, 0))

    async def _on_tp_fill(self, t: TrackedOrder, qty: float, fee: float) -> None:
        pos = self.positions.get(t.position_id or "")
        if pos is None:
            return
        px = t.avg_price or t.price
        pos.size = max(pos.size - qty, 0.0)
        pos.realized_gross += (px - pos.entry_price) * qty
        pos.exit_fees_paid += fee
        for lv in pos.tp_levels:
            if lv.client_order_id == t.client_order_id and t.status is OrderStatus.FILLED:
                lv.filled = True
        meta = self.gw.meta(pos.product_id)
        if t.status is OrderStatus.FILLED:
            self.notifier.notify_partial_tp(pos, px, qty, fee, self.mode)
        if pos.size < meta.base_min_size and not pos.closing:
            reason = "STOP EN EXCHANGE" if t.stop_trigger and px <= t.stop_trigger * 1.01 else "TAKE PROFIT"
            await self._finalize(pos, exit_price=px, reason=reason)
        else:
            await self._persist_position(pos)

    async def manage_positions(self) -> None:
        """Llamado en cada ciclo del motor: BE, trailing y stops."""
        trail_mult = self.s.strategy.trail_atr_mult
        be_trigger = self.s.strategy.breakeven_trigger_r
        for pos in list(self.positions.values()):
            if pos.closing:
                continue
            mk = self.markets[pos.product_id]
            bid = mk.book.best_bid
            if bid is None:
                continue
            pos.highest_price = max(pos.highest_price, bid)
            changed = False
            if pos.strategy is StrategyName.TREND:
                # Tendencia: trailing k·ATR(6h) desde el máximo, sin breakeven (igual que el backtest)
                st = self.strategy.trend.state.get(pos.product_id)
                atr6 = st.atr if st is not None and st.ready else pos.atr_at_entry
                trail = pos.highest_price - self.s.trend.k_atr * atr6
                if trail > pos.stop_price:
                    pos.stop_price = trail
                    changed = True
                if bid <= pos.stop_price:
                    asyncio.create_task(self.close_position(
                        pos.position_id, "TRAILING STOP" if pos.stop_price > pos.entry_price else "STOP LOSS"))
                    continue
                if changed:
                    await self._persist_position(pos)
                self._schedule_exchange_stop_update(pos)
                continue
            # 2) Fee-Adjusted Breakeven a +1.2R
            if not pos.breakeven_armed and pos.r_multiple(bid) >= be_trigger:
                be = fee_adjusted_breakeven(pos.entry_price, pos.entry_fee_rate, self.strategy.taker_fee)
                be = round_step(be, self.gw.meta(pos.product_id).quote_increment, up=True)
                if be > pos.stop_price:
                    pos.stop_price = be
                pos.breakeven_armed = True
                changed = True
                self.notifier.notify_breakeven(pos, be, self.mode)
            # 3) Trailing por ATR (sólo tras breakeven para no estrangular el trade)
            if pos.breakeven_armed:
                atr_now = self.strategy.analytics[pos.product_id].atr
                atr_use = atr_now if not math.isnan(atr_now) else pos.atr_at_entry
                trail = pos.highest_price - trail_mult * atr_use
                if trail > pos.stop_price:
                    pos.stop_price = trail
                    changed = True
            # 1) Stop (hard / BE / trailing)
            if bid <= pos.stop_price:
                kind = "TRAILING STOP" if pos.breakeven_armed and pos.stop_price > pos.entry_price else (
                    "BREAKEVEN STOP" if pos.breakeven_armed else "STOP LOSS")
                asyncio.create_task(self.close_position(pos.position_id, kind))
                continue
            if changed:
                await self._persist_position(pos)
            self._schedule_exchange_stop_update(pos)

    async def close_position(self, position_id: str, reason: str) -> None:
        pos = self.positions.get(position_id)
        if pos is None:
            return
        lock = self._close_locks.setdefault(position_id, asyncio.Lock())
        async with lock:
            if position_id not in self.positions or pos.closing:
                return
            pos.closing = True
            # 1) cancelar TPs en reposo (libera el inventario retenido)
            tps = [t for t in self.orders_by_cid.values()
                   if t.position_id == position_id and t.purpose == "TAKE_PROFIT" and not t.status.is_terminal]
            await asyncio.gather(*(self._cancel_and_settle(t) for t in tps))
            meta = self.gw.meta(pos.product_id)
            qty = round_step(pos.size, meta.base_increment)
            exit_px = self.markets[pos.product_id].book.best_bid or pos.entry_price
            if qty >= meta.base_min_size:
                t = TrackedOrder(
                    client_order_id=new_client_order_id(self.s.execution.order_prefix, "ex"),
                    product_id=pos.product_id, side=Side.SELL, purpose="EXIT",
                    price=0.0, size=qty, position_id=position_id,
                )
                self._track(t)
                for attempt in range(3):
                    try:
                        oid = await self.gw.place_market_sell(pos.product_id, qty, t.client_order_id)
                        self._bind(t, oid)
                        break
                    except ExchangeError as exc:
                        log.error("Salida %s intento %d falló: %s", pos.product_id, attempt + 1, exc)
                        await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    pos.closing = False
                    self.notifier.notify_risk(
                        f"⚠️ No se pudo cerrar {pos.product_id} ({reason}). Reintentando en el próximo ciclo.")
                    return
                if not await self._wait_terminal(t, 10.0):
                    await self._sync_from_rest(t)
                if t.filled > 0:
                    exit_px = t.avg_price or exit_px
                    pos.realized_gross += (exit_px - pos.entry_price) * t.filled
                    pos.exit_fees_paid += t.fees
                    pos.size = max(pos.size - t.filled, 0.0)
            await self._finalize(pos, exit_price=exit_px, reason=reason)

    async def _finalize(self, pos: Position, exit_price: float, reason: str) -> None:
        self.positions.pop(pos.position_id, None)
        for t in list(self.orders_by_cid.values()):
            if t.position_id == pos.position_id and not t.status.is_terminal and t.order_id:
                await self._cancel_and_settle(t)
        fees = pos.entry_fee_paid + pos.exit_fees_paid
        gross = pos.realized_gross
        net = gross - fees
        invested = pos.entry_price * pos.initial_size
        closed_qty = pos.initial_size - pos.size
        avg_exit = pos.entry_price + gross / closed_qty if closed_qty > 0 else exit_price
        trade = {
            "trade_id": uuid.uuid4().hex[:12],
            "position_id": pos.position_id,
            "product_id": pos.product_id,
            "strategy": pos.strategy.value,
            "entry_price": pos.entry_price,
            "exit_price": avg_exit,
            "size": pos.initial_size,
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "net_pct": net / invested * 100.0 if invested > 0 else 0.0,
            "opened_at": pos.opened_at,
            "closed_at": time.time(),
            "duration_s": time.time() - pos.opened_at,
            "exit_reason": reason,
            "mode": self.mode,
            "initial_stop": pos.initial_stop,
            "take_profit": pos.take_profit,
        }
        self.closed_trades_cache.append(trade)
        await self.db.run(self.db.sync.insert_trade, trade)
        row = self._position_row(pos, status="CLOSED")
        await self.db.run(self.db.sync.upsert_position, row)
        self.notifier.notify_close(pos, trade, self.mode)
        evt = self.risk.on_trade_closed(net)
        if evt:
            self.notifier.notify_risk(f"⏸️ {evt.message}")
            self.db.fire(self.db.sync.log_event, "WARN", evt.kind, evt.message)
        self.db.fire(self.db.sync.log_event, "INFO", "EXIT",
                     f"{pos.product_id} {reason} neto {net:+.2f} USDC")

    # ═════════════════════════════════════════════════════════════════════
    # Kill-Switch
    # ═════════════════════════════════════════════════════════════════════
    async def panic_flatten(self, reason: str) -> dict[str, Any]:
        t0 = time.time()
        # 1) detener entradas en curso
        for task in list(self.entry_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.entry_tasks.values(), return_exceptions=True)
        # 2) cancelar TODAS las órdenes abiertas del universo en el exchange
        cancelled = 0
        try:
            open_orders = await self.gw.list_open_orders(list(self.markets.keys()))
            ids = [o.order_id for o in open_orders]
            ids += [t.order_id for t in self.orders_by_cid.values()
                    if t.order_id and not t.status.is_terminal and t.order_id not in ids]
            if ids:
                await self.gw.cancel(ids)
                cancelled = len(ids)
        except Exception as exc:  # noqa: BLE001
            log.error("Kill-switch: error cancelando órdenes: %s", exc)
        # 3) liquidar posiciones
        await asyncio.gather(
            *(self.close_position(pid, f"KILL-SWITCH: {reason}") for pid in list(self.positions)),
            return_exceptions=True,
        )
        still_open = len(self.positions)
        summary = {
            "cancelled_orders": cancelled,
            "positions_remaining": still_open,
            "elapsed_s": round(time.time() - t0, 2),
        }
        self.db.fire(self.db.sync.log_event, "CRITICAL", "KILL_SWITCH", f"{reason}: {json.dumps(summary)}")
        return summary

    # ═════════════════════════════════════════════════════════════════════
    # Persistencia / reconciliación
    # ═════════════════════════════════════════════════════════════════════
    def _position_row(self, pos: Position, status: str = "OPEN") -> dict[str, Any]:
        mk = self.markets[pos.product_id]
        mark = mk.book.best_bid or mk.last_price
        unreal = self.unrealized_net(pos, mark)
        state = asdict(pos)
        state["strategy"] = pos.strategy.value
        state["regime"] = pos.regime.value
        return {
            "position_id": pos.position_id,
            "product_id": pos.product_id,
            "strategy": pos.strategy.value,
            "regime": pos.regime.value,
            "entry_price": pos.entry_price,
            "size": pos.size,
            "initial_size": pos.initial_size,
            "stop_price": pos.stop_price,
            "initial_stop": pos.initial_stop,
            "take_profit": pos.take_profit,
            "breakeven_price": fee_adjusted_breakeven(pos.entry_price, pos.entry_fee_rate, self.strategy.taker_fee),
            "breakeven_armed": int(pos.breakeven_armed),
            "mark_price": mark,
            "unrealized_net": unreal,
            "entry_fee_paid": pos.entry_fee_paid,
            "opened_at": pos.opened_at,
            "status": status,
            "mode": self.mode,
            "state_json": json.dumps(state),
            "updated_at": time.time(),
        }

    def unrealized_net(self, pos: Position, mark: float) -> float:
        """PnL neto flotante: incluye fees pagadas + fee taker estimada de salida."""
        if mark is None or math.isnan(mark):
            return 0.0
        gross_open = (mark - pos.entry_price) * pos.size
        exit_fee_est = mark * pos.size * self.strategy.taker_fee
        return pos.realized_gross + gross_open - pos.entry_fee_paid - pos.exit_fees_paid - exit_fee_est

    async def _persist_position(self, pos: Position) -> None:
        await self.db.run(self.db.sync.upsert_position, self._position_row(pos))

    def position_rows(self) -> list[dict[str, Any]]:
        return [self._position_row(p) for p in self.positions.values()]

    async def restore(self) -> None:
        """Recupera posiciones abiertas tras un reinicio y re-sincroniza sus TPs."""
        rows = await self.db.run(self.db.sync.open_positions, self.mode)
        for r in rows:
            st = json.loads(r["state_json"])
            st["strategy"] = StrategyName(st["strategy"])
            st["regime"] = Regime(st["regime"])
            st["tp_levels"] = [TPLevel(**lv) for lv in st.get("tp_levels", [])]
            st["closing"] = False
            pos = Position(**st)
            if pos.product_id not in self.markets:
                continue
            self.positions[pos.position_id] = pos
            for lv in pos.tp_levels:
                if lv.filled or not lv.order_id:
                    continue
                upd = None
                try:
                    upd = await self.gw.get_order(lv.order_id)
                except Exception:  # noqa: BLE001
                    upd = None
                if upd is None or upd.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                 OrderStatus.FAILED, OrderStatus.REJECTED):
                    lv.order_id = None
                    lv.client_order_id = None
                    continue
                t = TrackedOrder(
                    client_order_id=upd.client_order_id or lv.client_order_id or "",
                    product_id=pos.product_id, side=Side.SELL, purpose="TAKE_PROFIT",
                    price=lv.price, size=upd.filled_size + upd.leaves_size, position_id=pos.position_id,
                    order_id=upd.order_id, status=upd.status, filled=upd.filled_size,
                    avg_price=upd.avg_price, fees=upd.total_fees,
                )
                self._track(t)
                self.orders_by_oid[upd.order_id] = t
                if upd.status is OrderStatus.FILLED:
                    lv.filled = True
            log.info("Posición restaurada %s %s size=%s", pos.position_id, pos.product_id, pos.size)

    async def ensure_tp_orders(self) -> None:
        """Recoloca TPs ausentes (p. ej. tras reinicio o rechazo Post-Only)."""
        for pos in list(self.positions.values()):
            if pos.closing:
                continue
            active = [t for t in self.orders_by_cid.values()
                      if t.position_id == pos.position_id and t.purpose == "TAKE_PROFIT"
                      and not t.status.is_terminal]
            if not active:
                for lv in pos.tp_levels:
                    if not lv.filled:
                        lv.order_id = None
                await self._place_tp_orders(pos)
                await self._persist_position(pos)

    async def reconcile_open_orders(self) -> None:
        """Polling de seguridad: sincroniza órdenes vivas vía REST."""
        live = [t for t in self.orders_by_cid.values() if t.order_id and not t.status.is_terminal]
        for t in live:
            await self._sync_from_rest(t)
        # purga de órdenes terminales antiguas en memoria
        cutoff = time.time() - 3600
        for cid, t in list(self.orders_by_cid.items()):
            if t.status.is_terminal and t.created_at < cutoff:
                self.orders_by_cid.pop(cid, None)
                if t.order_id:
                    self.orders_by_oid.pop(t.order_id, None)
