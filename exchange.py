"""
exchange.py — Adaptadores de exchange.

* `CoinbaseGateway` (LIVE): envuelve `coinbase.rest.RESTClient` (síncrono) con
  `asyncio.to_thread`, mide latencia de cada llamada y normaliza respuestas.
* `PaperGateway` (PAPER): simulador de matching realista que consume el MISMO
  flujo de WebSocket en vivo: órdenes Post-Only con posición en cola L2,
  rechazo si cruzan el spread, ejecución a mercado recorriendo el libro y
  comisiones Maker/Taker reales. En perpetuos lleva posiciones netas
  (largo/corto), PnL realizado, margen y financiación.

Unidades: el resto del bot trabaja SIEMPRE con el par lógico (p. ej. BTC-USDC)
y tamaños en unidades del activo (BTC). El gateway traduce al producto real
(p. ej. BIP-20DEC30-CDE) y a contratos (1 contrato = 0.01 BTC).

Ambos implementan la interfaz `Gateway` y emiten `OrderUpdate` normalizados
hacia `on_order_update`, igual que el canal `user` de Coinbase.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Awaitable, Callable

from market_data import MarketState
from models import OrderStatus, OrderUpdate, Side

log = logging.getLogger("exchange")

OrderCallback = Callable[[OrderUpdate], Awaitable[None]]


class ExchangeError(Exception):
    pass


class PostOnlyRejected(ExchangeError):
    """La orden Post-Only habría cruzado el spread (sería Taker)."""


@dataclass
class ProductMeta:
    product_id: str               # par lógico (BTC-USDC)
    base_increment: float         # en unidades del activo (en perpetuos = tamaño del contrato)
    quote_increment: float        # tick de precio
    base_min_size: float
    quote_min_size: float
    contract_size: float = 1.0    # 1 en spot; p. ej. 0.01 BTC por contrato en BIP
    exchange_id: str = ""         # producto real en Coinbase
    is_future: bool = False


def meta_from_product(pid: str, p: dict[str, Any]) -> ProductMeta:
    if (p.get("product_type") or "").upper() == "FUTURE":
        cs = float(to_dict(p.get("future_product_details")).get("contract_size") or 1.0)
        return ProductMeta(
            product_id=pid, base_increment=cs * float(p["base_increment"]),
            quote_increment=float(p.get("price_increment") or p["quote_increment"]),
            base_min_size=cs * float(p["base_min_size"]), quote_min_size=0.0,
            contract_size=cs, exchange_id=p["product_id"], is_future=True,
        )
    return ProductMeta(
        product_id=pid, base_increment=float(p["base_increment"]),
        quote_increment=float(p.get("price_increment") or p["quote_increment"]),
        base_min_size=float(p["base_min_size"]), quote_min_size=float(p.get("quote_min_size") or 1.0),
        exchange_id=p.get("product_id", pid),
    )


def to_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    return dict(getattr(resp, "__dict__", {}))


def round_step(value: float, step: float, up: bool = False) -> float:
    if step <= 0:
        return value
    d_val, d_step = Decimal(str(value)), Decimal(str(step))
    q = (d_val / d_step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return float(q * d_step)


def fmt_step(value: float, step: float) -> str:
    decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)  # type: ignore[operator]
    return f"{value:.{decimals}f}"


def map_status(raw: str) -> OrderStatus:
    raw = (raw or "").upper()
    return {
        "PENDING": OrderStatus.PENDING,
        "OPEN": OrderStatus.OPEN,
        "FILLED": OrderStatus.FILLED,
        "CANCELLED": OrderStatus.CANCELLED,
        "CANCEL_QUEUED": OrderStatus.OPEN,
        "EXPIRED": OrderStatus.EXPIRED,
        "FAILED": OrderStatus.FAILED,
        "REJECTED": OrderStatus.REJECTED,
    }.get(raw, OrderStatus.OPEN)


def new_client_order_id(prefix: str, tag: str) -> str:
    return f"{prefix}-{tag}-{uuid.uuid4().hex[:16]}"


class Gateway(ABC):
    mode: str

    def __init__(self) -> None:
        self.on_order_update: OrderCallback | None = None
        self.last_latency_ms: float = 0.0
        self.products: dict[str, ProductMeta] = {}
        self.venue: dict[str, str] = {}          # par lógico → producto real

    # ── traducción par lógico ↔ producto real ──────────────────────────────
    def set_venue_map(self, venue: dict[str, str]) -> None:
        self.venue = dict(venue)

    def vid(self, pid: str) -> str:
        return self.venue.get(pid, pid)

    def lid(self, exchange_id: str) -> str:
        for k, v in self.venue.items():
            if v == exchange_id:
                return k
        return exchange_id

    def to_units(self, pid: str, exchange_qty: float) -> float:
        """Cantidad del exchange (contratos en perpetuos) → unidades del activo."""
        m = self.products.get(pid)
        return exchange_qty * (m.contract_size if m else 1.0)

    def _size_str(self, pid: str, base_size: float) -> str:
        m = self.meta(pid)
        units = round_step(base_size, m.base_increment) / m.contract_size
        return fmt_step(units, m.base_increment / m.contract_size)

    async def _emit(self, upd: OrderUpdate) -> None:
        if self.on_order_update is not None:
            await self.on_order_update(upd)

    @property
    def is_perps(self) -> bool:
        return any(m.is_future for m in self.products.values())

    @abstractmethod
    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]: ...

    @abstractmethod
    async def get_candles(self, product_id: str, minutes: int, granularity: str = "ONE_MINUTE",
                          seconds: int = 60) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_balances(self) -> dict[str, float]:
        """{'USDC': caja}. En perpetuos además '_unrealized' (PnL flotante de la cuenta)."""

    @abstractmethod
    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str: ...

    @abstractmethod
    async def place_market(self, product_id: str, side: Side, base_size: float, client_order_id: str) -> str: ...

    @abstractmethod
    async def place_bracket(
        self, product_id: str, side: Side, base_size: float, limit_price: float, stop_trigger: float,
        client_order_id: str,
    ) -> str:
        """Bracket GTC de cierre: límite (TP) + stop de protección residente en el exchange."""

    async def place_market_sell(self, product_id: str, base_size: float, client_order_id: str) -> str:
        return await self.place_market(product_id, Side.SELL, base_size, client_order_id)

    async def place_bracket_sell(
        self, product_id: str, base_size: float, limit_price: float, stop_trigger: float, client_order_id: str
    ) -> str:
        return await self.place_bracket(product_id, Side.SELL, base_size, limit_price, stop_trigger, client_order_id)

    @abstractmethod
    async def cancel(self, order_ids: list[str]) -> None: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> OrderUpdate | None: ...

    @abstractmethod
    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]: ...

    @abstractmethod
    async def ping(self) -> float: ...

    @abstractmethod
    async def fee_rates(self) -> tuple[float, float] | None: ...

    def meta(self, product_id: str) -> ProductMeta:
        return self.products[product_id]


# ═════════════════════════════════════════════════════════════════════════════
# LIVE — Coinbase Advanced Trade v3 (spot y perpetuos US de Coinbase Financial Markets)
# ═════════════════════════════════════════════════════════════════════════════
class CoinbaseGateway(Gateway):
    mode = "LIVE"

    def __init__(self, api_key: str, api_secret: str, timeout: int = 10) -> None:
        super().__init__()
        from coinbase.rest import RESTClient

        self.client = RESTClient(api_key=api_key, api_secret=api_secret, timeout=timeout)
        self._bracket_unsupported = False

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        return to_dict(resp)

    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]:
        for pid in product_ids:
            p = await self._call(self.client.get_product, self.vid(pid))
            self.products[pid] = meta_from_product(pid, p)
        return self.products

    async def get_candles(self, product_id: str, minutes: int, granularity: str = "ONE_MINUTE",
                          seconds: int = 60) -> list[dict[str, Any]]:
        return await _fetch_candles(self._call, self.client.get_candles, self.vid(product_id), minutes,
                                    granularity, seconds)

    async def get_balances(self) -> dict[str, float]:
        if self.is_perps:
            resp = await self._call(self.client.get_futures_balance_summary)
            bs = to_dict(resp.get("balance_summary"))

            def val(k: str) -> float:
                return float(to_dict(bs.get(k)).get("value") or 0.0)

            return {"USDC": val("total_usd_balance"), "_unrealized": val("unrealized_pnl"),
                    "_buying_power": val("futures_buying_power")}
        out: dict[str, float] = {}
        cursor: str | None = None
        while True:
            resp = await self._call(self.client.get_accounts, limit=250, cursor=cursor)
            for acc in resp.get("accounts", []):
                cur = acc["currency"]
                avail = float(acc["available_balance"]["value"])
                hold = float((acc.get("hold") or {}).get("value", 0.0))
                out[cur] = out.get(cur, 0.0) + avail + hold
            if not resp.get("has_next"):
                break
            cursor = resp.get("cursor")
        return out

    def _parse_create(self, resp: dict[str, Any]) -> str:
        if resp.get("success"):
            return str(to_dict(resp["success_response"])["order_id"])
        err = to_dict(resp.get("error_response"))
        reason = f"{err.get('error')} {err.get('message')} {err.get('preview_failure_reason')}"
        if "POST_ONLY" in reason.upper():
            raise PostOnlyRejected(reason)
        raise ExchangeError(reason)

    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str:
        m = self.meta(product_id)
        price_s = fmt_step(round_step(price, m.quote_increment, up=side is Side.SELL), m.quote_increment)
        fn = self.client.limit_order_gtc_buy if side is Side.BUY else self.client.limit_order_gtc_sell
        resp = await self._call(
            fn, client_order_id=client_order_id, product_id=self.vid(product_id),
            base_size=self._size_str(product_id, base_size), limit_price=price_s, post_only=True,
        )
        return self._parse_create(resp)

    async def place_market(self, product_id: str, side: Side, base_size: float, client_order_id: str) -> str:
        resp = await self._call(
            self.client.market_order, client_order_id=client_order_id, product_id=self.vid(product_id),
            side=side.value, base_size=self._size_str(product_id, base_size),
        )
        return self._parse_create(resp)

    async def place_bracket(
        self, product_id: str, side: Side, base_size: float, limit_price: float, stop_trigger: float,
        client_order_id: str,
    ) -> str:
        m = self.meta(product_id)
        q = m.quote_increment
        size_s = self._size_str(product_id, base_size)
        if not self._bracket_unsupported:
            try:
                resp = await self._call(
                    self.client.trigger_bracket_order_gtc, client_order_id=client_order_id,
                    product_id=self.vid(product_id), side=side.value, base_size=size_s,
                    limit_price=fmt_step(round_step(limit_price, q, up=side is Side.SELL), q),
                    stop_trigger_price=fmt_step(round_step(stop_trigger, q, up=side is Side.BUY), q),
                )
                return self._parse_create(resp)
            except ExchangeError as exc:
                if "UNSUPPORTED" not in str(exc).upper() and "INVALID_ORDER_CONFIG" not in str(exc).upper():
                    raise
                log.warning("Bracket no soportado en %s (%s): uso stop-limit", product_id, exc)
                self._bracket_unsupported = True
        # Alternativa: stop-limit GTC de protección (sin TP), límite 2 % más allá del disparo
        stop_s = fmt_step(round_step(stop_trigger, q, up=side is Side.BUY), q)
        lim = stop_trigger * (0.98 if side is Side.SELL else 1.02)
        resp = await self._call(
            self.client.stop_limit_order_gtc, client_order_id=client_order_id, product_id=self.vid(product_id),
            side=side.value, base_size=size_s, limit_price=fmt_step(round_step(lim, q, up=side is Side.BUY), q),
            stop_price=stop_s,
            stop_direction="STOP_DIRECTION_STOP_DOWN" if side is Side.SELL else "STOP_DIRECTION_STOP_UP",
        )
        return self._parse_create(resp)

    async def cancel(self, order_ids: list[str]) -> None:
        if not order_ids:
            return
        for i in range(0, len(order_ids), 100):  # límite del endpoint batch_cancel
            await self._call(self.client.cancel_orders, order_ids=order_ids[i:i + 100])

    def _to_update(self, o: dict[str, Any]) -> OrderUpdate:
        cfg = to_dict(o.get("order_configuration"))
        limit_price = 0.0
        for v in cfg.values():
            v = to_dict(v)
            if "limit_price" in v:
                limit_price = float(v["limit_price"])
        pid = self.lid(o["product_id"])
        return OrderUpdate(
            order_id=o["order_id"],
            client_order_id=o.get("client_order_id", ""),
            product_id=pid,
            side=Side(o["side"]),
            status=map_status(o.get("status", "")),
            filled_size=self.to_units(pid, float(o.get("filled_size") or 0.0)),
            avg_price=float(o.get("average_filled_price") or 0.0),
            total_fees=float(o.get("total_fees") or 0.0),
            leaves_size=self.to_units(pid, max(float(o.get("leaves_quantity") or 0.0), 0.0)),
            limit_price=limit_price,
        )

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        resp = await self._call(self.client.get_order, order_id)
        o = to_dict(resp.get("order"))
        return self._to_update(o) if o else None

    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]:
        resp = await self._call(self.client.list_orders, product_ids=[self.vid(p) for p in product_ids],
                                order_status=["OPEN"])
        return [self._to_update(to_dict(o)) for o in resp.get("orders", [])]

    async def ping(self) -> float:
        await self._call(self.client.get_unix_time)
        return self.last_latency_ms

    async def fee_rates(self) -> tuple[float, float] | None:
        kwargs = {"product_type": "FUTURE"} if self.is_perps else {}
        resp = await self._call(self.client.get_transaction_summary, **kwargs)
        tier = to_dict(resp.get("fee_tier"))
        if not tier:
            return None
        return float(tier["maker_fee_rate"]), float(tier["taker_fee_rate"])


async def _fetch_candles(
    caller: Callable[..., Awaitable[dict[str, Any]]], fn: Callable[..., Any], product_id: str, minutes: int,
    granularity: str = "ONE_MINUTE", seconds: int = 60,
) -> list[dict[str, Any]]:
    """Descarga velas paginando de 300 en 300 (límite API = 350), con backoff ante 429."""
    end = datetime.now(timezone.utc)
    start_limit = end - timedelta(minutes=minutes)
    out: list[dict[str, Any]] = []
    cursor_end = end
    while cursor_end > start_limit:
        chunk_start = max(cursor_end - timedelta(seconds=300 * seconds), start_limit)
        resp: dict[str, Any] = {}
        for attempt in range(6):
            try:
                resp = await caller(
                    fn, product_id=product_id,
                    start=str(int(chunk_start.timestamp())), end=str(int(cursor_end.timestamp())),
                    granularity=granularity,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if "429" not in str(exc) or attempt == 5:
                    raise
                await asyncio.sleep(1.5 * (2 ** attempt))   # rate limit: backoff exponencial
        out.extend(to_dict(c) for c in resp.get("candles", []))
        cursor_end = chunk_start
        await asyncio.sleep(0.35)   # < 3 req/s: bajo el límite de endpoints públicos
    return out


# ═════════════════════════════════════════════════════════════════════════════
# PAPER — simulador con matching sobre el libro real
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class _PaperOrder:
    order_id: str
    client_order_id: str
    product_id: str
    side: Side
    price: float
    size: float
    filled: float = 0.0
    notional: float = 0.0
    fees: float = 0.0
    queue_ahead: float = 0.0
    stop_trigger: float = 0.0          # >0 ⇒ orden bracket (stop de protección)
    status: OrderStatus = OrderStatus.OPEN

    @property
    def leaves(self) -> float:
        return max(self.size - self.filled, 0.0)

    @property
    def avg(self) -> float:
        return self.notional / self.filled if self.filled > 0 else 0.0


class PaperGateway(Gateway):
    """
    Spot: saldos por moneda (sólo largo). Perpetuos (`perps=True`): caja en USDC +
    posición neta por par (POS:<par>, AVG:<par>), PnL realizado al reducir,
    control de margen con apalancamiento máximo y cargo de financiación.
    """

    mode = "PAPER"

    def __init__(
        self,
        markets: dict[str, MarketState],
        starting_equity: float,
        maker_fee: float,
        taker_fee: float,
        quote: str = "USDC",
        perps: bool = False,
        max_leverage: float = 1.0,
    ) -> None:
        super().__init__()
        from coinbase.rest import RESTClient

        self.public = RESTClient(api_key=None, api_secret=None)  # endpoints públicos
        self.markets = markets
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.quote = quote
        self.perps_mode = perps
        self.max_leverage = max_leverage
        self.balances: dict[str, float] = {quote: starting_equity}
        self.orders: dict[str, _PaperOrder] = {}

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        return to_dict(resp)

    def restore_balances(self, balances: dict[str, float]) -> None:
        if balances:
            self.balances = {k: float(v) for k, v in balances.items()}

    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]:
        for pid in product_ids:
            p = await self._call(self.public.get_public_product, self.vid(pid))
            self.products[pid] = meta_from_product(pid, p)
        return self.products

    async def get_candles(self, product_id: str, minutes: int, granularity: str = "ONE_MINUTE",
                          seconds: int = 60) -> list[dict[str, Any]]:
        return await _fetch_candles(self._call, self.public.get_public_candles, self.vid(product_id), minutes,
                                    granularity, seconds)

    # ── posiciones (perpetuos) ──────────────────────────────────────────────
    def position(self, pid: str) -> tuple[float, float]:
        return self.balances.get(f"POS:{pid}", 0.0), self.balances.get(f"AVG:{pid}", 0.0)

    def _mark(self, pid: str) -> float:
        mk = self.markets[pid]
        return mk.book.mid or mk.last_price

    def unrealized(self) -> float:
        tot = 0.0
        for k, qty in self.balances.items():
            if k.startswith("POS:") and qty:
                pid = k[4:]
                px = self._mark(pid)
                if px and not math.isnan(px):
                    tot += (px - self.balances.get(f"AVG:{pid}", 0.0)) * qty
        return tot

    def open_notional(self) -> float:
        tot = 0.0
        for k, qty in self.balances.items():
            if k.startswith("POS:") and qty:
                px = self._mark(k[4:])
                tot += abs(qty) * (px if px and not math.isnan(px) else 0.0)
        return tot

    def charge_funding(self, rate: float) -> float:
        """Cargo de financiación (modelo conservador: lo paga cualquier posición)."""
        cost = self.open_notional() * rate
        self.balances[self.quote] = self.balances.get(self.quote, 0.0) - cost
        return cost

    def _apply_fill(self, pid: str, side: Side, qty: float, price: float, fee: float) -> None:
        cash = self.balances.get(self.quote, 0.0) - fee
        if not self.perps_mode:
            base = pid.split("-")[0]
            if side is Side.BUY:
                cash -= qty * price
                self.balances[base] = self.balances.get(base, 0.0) + qty
            else:
                cash += qty * price
                self.balances[base] = self.balances.get(base, 0.0) - qty
            self.balances[self.quote] = cash
            return
        pos, avg = self.position(pid)
        signed = qty if side is Side.BUY else -qty
        if pos == 0 or (pos > 0) == (signed > 0):          # abre o aumenta
            new = pos + signed
            avg = (avg * abs(pos) + price * qty) / abs(new)
            pos = new
        else:                                              # reduce, cierra o invierte
            closing = min(abs(signed), abs(pos))
            cash += (price - avg) * closing * (1 if pos > 0 else -1)
            pos += signed
            if abs(pos) < 1e-12:
                pos, avg = 0.0, 0.0
            elif (pos > 0) == (signed > 0):                # invirtió el sentido
                avg = price
        self.balances[f"POS:{pid}"] = pos
        self.balances[f"AVG:{pid}"] = avg
        self.balances[self.quote] = cash

    async def get_balances(self) -> dict[str, float]:
        if self.perps_mode:
            return {self.quote: self.balances.get(self.quote, 0.0), "_unrealized": self.unrealized()}
        return dict(self.balances)

    def _available(self, currency: str) -> float:
        held = 0.0
        for o in self.orders.values():
            if o.status is not OrderStatus.OPEN:
                continue
            base = o.product_id.split("-")[0]
            if o.side is Side.BUY and currency == self.quote:
                held += o.leaves * o.price * (1 + self.maker_fee)
            elif o.side is Side.SELL and currency == base:
                held += o.leaves
        return self.balances.get(currency, 0.0) - held

    def _check_funds(self, pid: str, side: Side, size: float, price: float, protective: bool = False) -> None:
        if not self.perps_mode:
            base = pid.split("-")[0]
            if side is Side.BUY and not protective and self._available(self.quote) < size * price * (1 + self.maker_fee):
                raise ExchangeError("INSUFFICIENT_FUND")
            if side is Side.SELL and self._available(base) < size - 1e-12:
                raise ExchangeError("INSUFFICIENT_FUND")
            return
        pos, _ = self.position(pid)
        reduces = (pos > 0 and side is Side.SELL) or (pos < 0 and side is Side.BUY)
        if reduces or protective:
            return
        equity = self.balances.get(self.quote, 0.0) + self.unrealized()
        if self.open_notional() + size * price > equity * self.max_leverage + 1e-6:
            raise ExchangeError("INSUFFICIENT_MARGIN")

    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str:
        t0 = time.perf_counter()
        m = self.meta(product_id)
        mk = self.markets[product_id]
        size = round_step(base_size, m.base_increment)
        price = round_step(price, m.quote_increment, up=side is Side.SELL)
        bb, ba = mk.book.best_bid, mk.book.best_ask
        if bb is None or ba is None:
            raise ExchangeError("Libro no inicializado")
        if size < m.base_min_size:
            raise ExchangeError(f"Tamaño {size} < mínimo {m.base_min_size}")
        if (side is Side.BUY and price >= ba) or (side is Side.SELL and price <= bb):
            raise PostOnlyRejected("INVALID_LIMIT_PRICE_POST_ONLY")
        self._check_funds(product_id, side, size, price)
        book_side = mk.book.bids if side is Side.BUY else mk.book.asks
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}",
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            price=price,
            size=size,
            queue_ahead=book_side.get(price, 0.0),  # FIFO: nos ponemos detrás
        )
        self.orders[order.order_id] = order
        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        await self._emit(self._update(order))
        return order.order_id

    async def place_market(self, product_id: str, side: Side, base_size: float, client_order_id: str) -> str:
        m = self.meta(product_id)
        mk = self.markets[product_id]
        size = round_step(base_size, m.base_increment)
        if not self.perps_mode and side is Side.SELL:
            size = min(size, self._available(product_id.split("-")[0]))
        if size <= 0:
            raise ExchangeError("INSUFFICIENT_FUND")
        bids, asks = mk.book.top(200)
        levels = bids if side is Side.SELL else asks
        remaining, notional = size, 0.0
        for px, qty in levels:
            take = min(remaining, qty)
            notional += take * px
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > 1e-12:  # libro insuficiente: resto al peor precio con guardia
            ref = levels[-1][0] if levels else mk.last_price
            notional += remaining * ref * (0.995 if side is Side.SELL else 1.005)
        fee = notional * self.taker_fee
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}", client_order_id=client_order_id,
            product_id=product_id, side=side, price=0.0, size=size,
            filled=size, notional=notional, fees=fee, status=OrderStatus.FILLED,
        )
        self._apply_fill(product_id, side, size, notional / size, fee)
        self.orders[order.order_id] = order
        await self._emit(self._update(order))
        return order.order_id

    async def place_bracket(
        self, product_id: str, side: Side, base_size: float, limit_price: float, stop_trigger: float,
        client_order_id: str,
    ) -> str:
        m = self.meta(product_id)
        size = round_step(base_size, m.base_increment)
        if size < m.base_min_size:
            raise ExchangeError("INSUFFICIENT_FUND")
        self._check_funds(product_id, side, size, limit_price, protective=True)
        book = self.markets[product_id].book
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}", client_order_id=client_order_id, product_id=product_id,
            side=side, price=round_step(limit_price, m.quote_increment, up=side is Side.SELL), size=size,
            stop_trigger=round_step(stop_trigger, m.quote_increment, up=side is Side.BUY),
            queue_ahead=(book.asks if side is Side.SELL else book.bids).get(limit_price, 0.0),
        )
        self.orders[order.order_id] = order
        await self._emit(self._update(order))
        return order.order_id

    async def cancel(self, order_ids: list[str]) -> None:
        for oid in order_ids:
            o = self.orders.get(oid)
            if o and o.status is OrderStatus.OPEN:
                o.status = OrderStatus.CANCELLED
                await self._emit(self._update(o))

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        o = self.orders.get(order_id)
        return self._update(o) if o else None

    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]:
        return [
            self._update(o) for o in self.orders.values()
            if o.status is OrderStatus.OPEN and o.product_id in product_ids
        ]

    async def ping(self) -> float:
        await self._call(self.public.get_unix_time)
        return self.last_latency_ms

    async def fee_rates(self) -> tuple[float, float] | None:
        return self.maker_fee, self.taker_fee

    def _update(self, o: _PaperOrder) -> OrderUpdate:
        return OrderUpdate(
            order_id=o.order_id, client_order_id=o.client_order_id, product_id=o.product_id,
            side=o.side, status=o.status, filled_size=o.filled, avg_price=o.avg,
            total_fees=o.fees, leaves_size=o.leaves, limit_price=o.price,
        )

    async def on_market_trade(self, product_id: str, price: float, size: float) -> None:
        """Matching de órdenes contra el flujo de trades real."""
        for o in list(self.orders.values()):
            if o.status is not OrderStatus.OPEN or o.product_id != product_id:
                continue
            triggered = o.stop_trigger > 0 and (
                (o.side is Side.SELL and price <= o.stop_trigger) or (o.side is Side.BUY and price >= o.stop_trigger))
            if triggered:
                # disparo del stop: ejecución a mercado (taker) al precio negociado
                fill = o.leaves
                fee = fill * price * self.taker_fee
                o.filled += fill
                o.notional += fill * price
                o.fees += fee
                self._apply_fill(product_id, o.side, fill, price, fee)
                o.status = OrderStatus.FILLED
                await self._emit(self._update(o))
                continue
            crossed = (o.side is Side.BUY and price < o.price) or (o.side is Side.SELL and price > o.price)
            at_level = math.isclose(price, o.price, rel_tol=0, abs_tol=1e-12)
            fill = 0.0
            if crossed:
                fill = o.leaves                      # el precio atravesó nuestro nivel
            elif at_level:
                consume = size
                if o.queue_ahead > 0:
                    eat = min(o.queue_ahead, consume)
                    o.queue_ahead -= eat
                    consume -= eat
                fill = min(o.leaves, consume)
            if fill <= 0:
                continue
            fee = fill * o.price * self.maker_fee
            o.filled += fill
            o.notional += fill * o.price
            o.fees += fee
            self._apply_fill(product_id, o.side, fill, o.price, fee)
            if o.leaves <= 1e-12:
                o.status = OrderStatus.FILLED
            await self._emit(self._update(o))
