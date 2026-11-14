"""
main.py — Core Engine: orquestador asíncrono.

Tareas concurrentes (asyncio):
    ws_dispatch     — consume la cola de mensajes WebSocket (level2, market_trades,
                      ticker, heartbeats, user) y actualiza el estado en memoria.
    eval_loop       — régimen + estrategias + oráculo BTC + veto de riesgo → entradas.
    manage_loop     — gestión tick a tick: Breakeven, trailing, stops.
    command_loop    — bus de comandos (Dashboard / Telegram). PANIC con prioridad.
    snapshot_loop   — publica telemetría en SQLite WAL para el Dashboard.
    equity_loop     — equity, Daily Drawdown, persistencia de estado de riesgo.
    watchdog_loop   — heartbeats + latencia REST → modo SAFE.
    reconcile_loop  — sincronización REST de órdenes y recolocación de TPs.
    ws_supervisor   — reconexión de WebSockets caídos y resync L2 ante huecos.

Los clientes WebSocket del SDK `coinbase-advanced-py` corren en su propio hilo
con su propio event loop; cada mensaje se parsea allí y se transfiere a este
loop con `call_soon_threadsafe` (cero bloqueo del motor).

Uso:  python main.py            (modo según TRADING_MODE en .env)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import signal as os_signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import ORACLE_PRODUCT, SETTINGS, Settings
from database import AsyncDatabase
from exchange import CoinbaseGateway, Gateway, PaperGateway, map_status
from execution_handler import ExecutionHandler
from market_data import MarketState, SequenceTracker, parse_ts
from models import BotStatus, CommandType, OrderUpdate, Regime, Side, Signal
from risk_manager import RiskManager, RiskState
from strategy_engine import StrategyEngine
from telegram_bot import TelegramNotifier

log = logging.getLogger("engine")


@dataclass
class WSConn:
    name: str
    channels: list[str]
    products: list[str]
    authenticated: bool
    user: bool = False
    client: Any = None
    connected: bool = False
    last_msg: float = 0.0
    last_heartbeat: float = 0.0
    seq: SequenceTracker = field(default_factory=SequenceTracker)
    resync_requested: bool = False
    reconnecting: bool = False


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.db = AsyncDatabase(settings.runtime.db_path)
        self.markets: dict[str, MarketState] = {
            pid: MarketState(pid, max_bars=settings.strategy.history_bars_1m) for pid in settings.pairs
        }
        self.strategy = StrategyEngine(settings.strategy, settings.fees, settings.oracle, settings.pairs)
        self.risk = RiskManager(settings.risk, settings.pairs, settings.execution.min_net_rr)
        self.notifier = TelegramNotifier(settings.creds.telegram_token, settings.creds.telegram_chat_id)
        self.mode = self._initial_mode()
        self.gateway: Gateway = self._build_gateway(self.mode)
        self.execution = self._build_execution()
        self.ws_queue: asyncio.Queue[tuple[str, dict[str, Any], float]] = asyncio.Queue(maxsize=100_000)
        self.conns: dict[str, WSConn] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tasks: list[asyncio.Task[Any]] = []
        self.stop_event = asyncio.Event()
        self.ws_latency_ms = 0.0
        self.api_latency_ms = 0.0
        self.equity = 0.0
        self.cash = 0.0
        self.balances: dict[str, float] = {}
        self._last_signal_ts: dict[str, float] = {}

    # ═════════════════════════════════════════════════════════════════════
    # Construcción
    # ═════════════════════════════════════════════════════════════════════
    def _initial_mode(self) -> str:
        override = self.db.sync.get_kv("mode_override")
        mode = (override or self.s.runtime.mode).upper()
        if mode == "LIVE" and not (self.s.runtime.allow_live and self.s.creds.has_coinbase):
            log.warning("LIVE solicitado sin ALLOW_LIVE_TRADING=true o sin credenciales → PAPER")
            mode = "PAPER"
        return mode

    def _build_gateway(self, mode: str) -> Gateway:
        if mode == "LIVE":
            return CoinbaseGateway(self.s.creds.coinbase_api_key, self.s.creds.coinbase_api_secret)
        gw = PaperGateway(
            self.markets, self.s.runtime.paper_starting_equity,
            self.s.fees.maker_fee, self.s.fees.taker_fee,
        )
        gw.restore_balances(self.db.sync.get_kv("paper_balances") or {})
        return gw

    def _build_execution(self) -> ExecutionHandler:
        return ExecutionHandler(
            self.s, self.gateway, self.markets, self.strategy, self.risk,
            self.db, self.notifier, self.validate_signal,
        )

    # ═════════════════════════════════════════════════════════════════════
    # Arranque
    # ═════════════════════════════════════════════════════════════════════
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.risk.state = RiskState.from_json(await self.db.run(self.db.sync.get_kv, "risk_state"))
        await self._publish_kv({"engine_started_at": time.time(), "mode": self.mode})
        await self._event("INFO", "BOOT", f"Arrancando Core Engine en modo {self.mode}")

        await self._load_market_metadata()
        await self._refresh_fee_rates()
        await self._seed_history()

        self._start_market_ws()
        if self.mode == "LIVE":
            self._start_user_ws()

        await self.execution.restore()
        await self.notifier.start(self.status_text, self.enqueue_command)
        self.notifier.notify_text(
            f"🚀 <b>Core Engine iniciado</b> · modo <b>{self.mode}</b>\n"
            f"Universo: {', '.join(self.s.pairs)}\nEstado: {self.risk.status.value}",
            critical=True,
        )
        self.tasks = [
            asyncio.create_task(self.ws_dispatch(), name="ws_dispatch"),
            asyncio.create_task(self.eval_loop(), name="eval_loop"),
            asyncio.create_task(self.manage_loop(), name="manage_loop"),
            asyncio.create_task(self.command_loop(), name="command_loop"),
            asyncio.create_task(self.snapshot_loop(), name="snapshot_loop"),
            asyncio.create_task(self.equity_loop(), name="equity_loop"),
            asyncio.create_task(self.watchdog_loop(), name="watchdog_loop"),
            asyncio.create_task(self.reconcile_loop(), name="reconcile_loop"),
            asyncio.create_task(self.ws_supervisor(), name="ws_supervisor"),
        ]
        for t in self.tasks:
            t.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self.stop_event.is_set():
            return
        exc = task.exception()
        if exc:
            log.critical("Tarea %s murió: %r — activando modo seguro", task.get_name(), exc)
            self.risk.state.safe_mode = True
            self.risk.state.safe_reason = f"tarea {task.get_name()} caída"
            self.notifier.notify_risk(f"Tarea crítica {task.get_name()} caída: {exc!r}. Reiniciando tarea.")
            # auto-restart de la tarea
            coro = getattr(self, task.get_name())()
            new = asyncio.create_task(coro, name=task.get_name())
            new.add_done_callback(self._task_done)
            self.tasks.append(new)

    async def _load_market_metadata(self) -> None:
        for attempt in range(1, 11):
            try:
                metas = await self.gateway.load_products(list(self.s.pairs))
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 10:
                    raise
                wait = min(2 ** attempt, 60)
                log.error("load_products falló (%d/10): %s — reintento en %ds", attempt, exc, wait)
                await asyncio.sleep(wait)
        for pid, m in metas.items():
            mk = self.markets[pid]
            mk.base_increment = m.base_increment
            mk.quote_increment = m.quote_increment
            mk.base_min_size = m.base_min_size

    async def _refresh_fee_rates(self) -> None:
        if self.mode != "LIVE" or not self.s.fees.fetch_live_fee_tier:
            return
        try:
            rates = await self.gateway.fee_rates()
            if rates:
                self.strategy.set_fee_rates(*rates)
                log.info("Fee tier real: maker=%.4f taker=%.4f (aplicado: %.4f / %.4f)",
                         rates[0], rates[1], self.strategy.maker_fee, self.strategy.taker_fee)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo leer el fee tier: %s", exc)

    async def _seed_history(self) -> None:
        minutes = self.s.strategy.history_bars_1m

        async def seed(pid: str) -> None:
            try:
                candles = await self.gateway.get_candles(pid, minutes)
                self.markets[pid].seed_candles(candles)
                log.info("Historia %s: %d velas 1m", pid, len(self.markets[pid].bars))
            except Exception as exc:  # noqa: BLE001
                log.error("Sin historia para %s: %s (calentamiento en vivo)", pid, exc)

        await asyncio.gather(*(seed(p) for p in self.s.pairs))

    # ═════════════════════════════════════════════════════════════════════
    # WebSockets (hilo del SDK → event loop del motor)
    # ═════════════════════════════════════════════════════════════════════
    def _ws_keys(self) -> tuple[str | None, str | None]:
        c = self.s.creds
        return (c.coinbase_api_key, c.coinbase_api_secret) if c.has_coinbase else (None, None)

    def _make_client(self, conn: WSConn) -> Any:
        from coinbase.websocket import WSClient, WSUserClient

        key, secret = self._ws_keys() if conn.authenticated else (None, None)
        loop = self.loop
        assert loop is not None

        def on_message(raw: str) -> None:
            recv = time.time()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return
            loop.call_soon_threadsafe(self._enqueue_ws, conn.name, msg, recv)

        def on_open() -> None:
            loop.call_soon_threadsafe(self._set_conn, conn.name, True)

        def on_close() -> None:
            loop.call_soon_threadsafe(self._set_conn, conn.name, False)

        cls = WSUserClient if conn.user else WSClient
        return cls(api_key=key, api_secret=secret, on_message=on_message,
                   on_open=on_open, on_close=on_close, retry=True)

    def _enqueue_ws(self, name: str, msg: dict[str, Any], recv: float) -> None:
        try:
            self.ws_queue.put_nowait((name, msg, recv))
        except asyncio.QueueFull:
            log.error("Cola WS llena: forzando resync de %s", name)
            self.conns[name].resync_requested = True

    def _set_conn(self, name: str, connected: bool) -> None:
        c = self.conns.get(name)
        if c:
            c.connected = connected
            if connected:
                c.seq.reset()

    def _open_conn(self, conn: WSConn) -> None:
        """Bloqueante (se llama vía to_thread o en arranque)."""
        conn.client = self._make_client(conn)
        conn.client.open()
        # Coinbase recomienda heartbeats en cada conexión para mantenerla viva
        conn.client.subscribe(product_ids=conn.products, channels=conn.channels + ["heartbeats"])
        conn.last_msg = conn.last_heartbeat = time.time()

    def _start_market_ws(self) -> None:
        products = list(self.s.pairs)
        auth = self.s.creds.has_coinbase
        # level2 en conexión dedicada (alto volumen) para aislar el resto de canales
        self.conns["l2"] = WSConn("l2", ["level2"], products, authenticated=auth)
        self.conns["flow"] = WSConn("flow", ["market_trades", "ticker"], products, authenticated=auth)
        for c in self.conns.values():
            if not c.user:
                self._open_conn(c)

    def _start_user_ws(self) -> None:
        conn = WSConn("user", ["user"], list(self.s.pairs), authenticated=True, user=True)
        self.conns["user"] = conn
        self._open_conn(conn)

    def _stop_user_ws(self) -> None:
        conn = self.conns.pop("user", None)
        if conn and conn.client:
            try:
                conn.client.close()
            except Exception:  # noqa: BLE001
                pass

    async def _reconnect(self, conn: WSConn, reason: str) -> None:
        if conn.reconnecting:
            return
        conn.reconnecting = True
        log.warning("Reconectando WS %s: %s", conn.name, reason)
        await self._event("WARN", "WS", f"Reconectando {conn.name}: {reason}")
        try:
            if conn.client:
                try:
                    await asyncio.to_thread(conn.client.close)
                except Exception:  # noqa: BLE001
                    pass
            if "level2" in conn.channels:
                for mk in self.markets.values():
                    mk.book.initialized = False      # esperar snapshot nuevo
            conn.seq.reset()
            await asyncio.to_thread(self._open_conn, conn)
            conn.resync_requested = False
        except Exception as exc:  # noqa: BLE001
            log.error("Reconexión %s falló: %s", conn.name, exc)
        finally:
            conn.reconnecting = False

    async def ws_supervisor(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(2.0)
            now = time.time()
            for conn in list(self.conns.values()):
                if conn.reconnecting:
                    continue
                if conn.resync_requested:
                    await self._reconnect(conn, "hueco de secuencia / resync L2")
                elif now - conn.last_msg > 15.0:
                    await self._reconnect(conn, f"sin mensajes {now - conn.last_msg:.0f}s")
                elif conn.client is not None and getattr(conn.client, "_background_exception", None):
                    await self._reconnect(conn, "excepción en el cliente WS")

    async def ws_dispatch(self) -> None:
        while True:
            name, msg, recv = await self.ws_queue.get()
            conn = self.conns.get(name)
            if conn is None:
                continue
            conn.last_msg = recv
            if not conn.seq.check(msg.get("sequence_num")):
                if "level2" in conn.channels:
                    conn.resync_requested = True
            ts = msg.get("timestamp")
            if ts:
                lat = (recv - parse_ts(ts)) * 1000.0
                if 0 <= lat < 60_000:
                    self.ws_latency_ms = 0.9 * self.ws_latency_ms + 0.1 * lat if self.ws_latency_ms else lat
            channel = msg.get("channel")
            try:
                if channel == "l2_data":
                    self._on_l2(msg)
                elif channel == "market_trades":
                    await self._on_trades(msg)
                elif channel == "ticker":
                    self._on_ticker(msg)
                elif channel == "heartbeats":
                    conn.last_heartbeat = recv
                elif channel == "user":
                    await self._on_user(msg)
                elif msg.get("type") == "error":
                    log.error("WS %s error: %s", name, msg.get("message"))
            except Exception:  # noqa: BLE001
                log.exception("Error procesando mensaje %s", channel)

    def _on_l2(self, msg: dict[str, Any]) -> None:
        for ev in msg.get("events", []):
            mk = self.markets.get(ev.get("product_id", ""))
            if mk is None:
                continue
            if ev.get("type") == "snapshot":
                mk.book.apply_snapshot(ev.get("updates", []))
            else:
                mk.book.apply_updates(ev.get("updates", []))

    async def _on_trades(self, msg: dict[str, Any]) -> None:
        taker = self.s.execution.trade_side_is_taker
        for ev in msg.get("events", []):
            is_update = ev.get("type") == "update"
            trades = sorted(ev.get("trades", []), key=lambda t: parse_ts(t.get("time")))
            for tr in trades:
                mk = self.markets.get(tr.get("product_id", ""))
                if mk is None:
                    continue
                price, size = float(tr["price"]), float(tr["size"])
                ts = parse_ts(tr.get("time"))
                buy = (tr.get("side") == "BUY") == taker
                if ts <= mk.last_trade_ts - 1.0 and not is_update:
                    continue
                mk.on_trade(ts, price, size, aggressor_buy=buy)
                if is_update and isinstance(self.gateway, PaperGateway):
                    await self.gateway.on_market_trade(mk.product_id, price, size)

    def _on_ticker(self, msg: dict[str, Any]) -> None:
        for ev in msg.get("events", []):
            for tk in ev.get("tickers", []):
                mk = self.markets.get(tk.get("product_id", ""))
                if mk is None:
                    continue
                mk.ticker_ts = time.time()
                try:
                    mk.volume_24h = float(tk.get("volume_24_h") or 0.0)
                except ValueError:
                    pass

    async def _on_user(self, msg: dict[str, Any]) -> None:
        for ev in msg.get("events", []):
            for o in ev.get("orders", []):
                if o.get("product_id") not in self.markets:
                    continue
                cum = float(o.get("cumulative_quantity") or 0.0)
                upd = OrderUpdate(
                    order_id=o.get("order_id", ""),
                    client_order_id=o.get("client_order_id", ""),
                    product_id=o["product_id"],
                    side=Side(o.get("order_side", "BUY").upper()),
                    status=map_status(o.get("status", "")),
                    filled_size=cum,
                    avg_price=float(o.get("avg_price") or 0.0),
                    total_fees=float(o.get("total_fees") or 0.0),
                    leaves_size=float(o.get("leaves_quantity") or 0.0),
                    limit_price=float(o.get("limit_price") or 0.0),
                )
                await self.execution.on_order_update(upd)

    # ═════════════════════════════════════════════════════════════════════
    # Señales
    # ═════════════════════════════════════════════════════════════════════
    def validate_signal(self, sig: Signal) -> tuple[bool, str]:
        """Revalidación ligera usada por el Smart Order Chasing."""
        a = self.strategy.analytics[sig.product_id]
        pcfg = self.s.pairs[sig.product_id]
        if time.time() - sig.ts > 120:
            return False, "señal caducada (>120s)"
        if a.regime is not sig.regime:
            return False, f"régimen cambió a {a.regime.value}"
        mk = self.markets[sig.product_id]
        obi = mk.book.obi(self.s.strategy.obi_levels)
        if obi < self.s.strategy.obi_min_long:
            return False, f"OBI {obi:+.2f}"
        sp = mk.book.spread_pct
        if sp is None or sp > pcfg.max_spread_pct:
            return False, f"spread {sp}"
        if sig.product_id != ORACLE_PRODUCT and self.strategy.oracle.state.veto:
            return False, "veto oráculo BTC"
        return True, "OK"

    async def eval_loop(self) -> None:
        interval = self.s.strategy.eval_interval_s
        while not self.stop_event.is_set():
            t0 = time.perf_counter()
            btc = self.markets.get(ORACLE_PRODUCT)
            if btc is not None and btc.book.initialized:
                st, changed = self.strategy.update_oracle(btc)
                if changed:
                    self.notifier.notify_oracle(st.veto, st.reason)
                    await self._event("WARN" if st.veto else "INFO", "ORACLE", st.reason)
            trades_hist: list[dict[str, Any]] | None = None
            for pid, mk in self.markets.items():
                analytics, sig = self.strategy.evaluate(mk)
                if sig is None:
                    continue
                ok, why = self.risk.can_open(pid, self.execution.open_products, self.execution.pending_products)
                if not ok:
                    analytics.signal_state = f"VETO RIESGO: {why}"
                    continue
                if time.time() - self._last_signal_ts.get(pid, 0.0) < 30:
                    continue
                if trades_hist is None:
                    trades_hist = await self.db.run(self.db.sync.trades, self.mode)
                sizing = self.risk.size_position(
                    sig, sig.ref_price, self.equity, self._free_quote(),
                    self.strategy.maker_fee, self.strategy.taker_fee,
                    self.gateway.meta(pid), trades_hist,
                )
                if not sizing.ok:
                    analytics.signal_state = f"VETO SIZING: {sizing.reason}"
                    continue
                self._last_signal_ts[pid] = time.time()
                analytics.signal_state = f"EJECUTANDO {sig.strategy.value}"
                await self._event("INFO", "SIGNAL",
                                  f"{pid} {sig.strategy.value} ref={sig.ref_price:.6g} SL={sig.stop_price:.6g} "
                                  f"TP={sig.take_profit:.6g} size={sizing.base_size} ({sizing.method})")
                self.execution.submit_entry(sig, sizing)
            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(interval - elapsed, 0.05))

    def _free_quote(self) -> float:
        cash = self.balances.get("USDC", 0.0)
        reserved = sum(
            t.size * t.price for t in self.execution.orders_by_cid.values()
            if t.side is Side.BUY and not t.status.is_terminal
        )
        return max(cash - reserved, 0.0)

    async def manage_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.execution.manage_positions()
            await asyncio.sleep(0.25)

    # ═════════════════════════════════════════════════════════════════════
    # Comandos
    # ═════════════════════════════════════════════════════════════════════
    async def enqueue_command(self, cmd: CommandType, source: str, payload: dict[str, Any] | None = None) -> None:
        await self.db.run(self.db.sync.push_command, cmd.value, source, payload or {})

    async def command_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                pending = await self.db.run(self.db.sync.pending_commands)
                for c in pending:
                    result = await self._execute_command(c)
                    await self.db.run(self.db.sync.ack_command, c["id"], result)
                    if c["command"] == CommandType.PANIC.value:
                        # tras un PANIC se relee la cola: el estado ha cambiado por completo
                        break
            except Exception:  # noqa: BLE001
                log.exception("Error en command_loop")
            await asyncio.sleep(self.s.runtime.command_poll_s)

    async def _execute_command(self, c: dict[str, Any]) -> str:
        cmd, src = c["command"], c.get("source", "?")
        log.warning("Comando %s desde %s", cmd, src)
        if cmd == CommandType.PANIC.value:
            return await self.kill_switch(f"PANIC desde {src}")
        if cmd == CommandType.PAUSE.value:
            self.risk.pause()
            await self._persist_risk()
            self.notifier.notify_text(f"⏸️ <b>Entradas PAUSADAS</b> ({src}). Posiciones abiertas siguen gestionadas.",
                                      critical=True)
            await self._event("WARN", "CONTROL", f"PAUSE ({src})")
            return "OK: pausado"
        if cmd == CommandType.RESUME.value:
            ok, msg = self.risk.resume()
            await self._persist_risk()
            self.notifier.notify_text(f"{'▶️' if ok else '⛔'} <b>RESUME</b> ({src}): {msg}", critical=True)
            await self._event("INFO" if ok else "WARN", "CONTROL", f"RESUME ({src}): {msg}")
            return ("OK: " if ok else "RECHAZADO: ") + msg
        if cmd == CommandType.SET_MODE.value:
            return await self._switch_mode(str(c["payload"].get("mode", "")).upper(), src)
        return f"Desconocido: {cmd}"

    async def kill_switch(self, reason: str, lock_reason: str = "panic") -> str:
        if lock_reason == "panic":
            self.risk.panic()
        await self._persist_risk()
        await self._publish_kv({"bot_status": BotStatus.KILLED.value})
        self.notifier.notify_text(f"🚨 <b>KILL-SWITCH ACTIVADO</b>\n{reason}\nCancelando órdenes y liquidando…",
                                  critical=True)
        summary = await self.execution.panic_flatten(reason)
        await self._refresh_equity()
        msg = (
            f"🧯 <b>CIERRE TOTAL CONFIRMADO</b>\n"
            f"Órdenes canceladas: {summary['cancelled_orders']}\n"
            f"Posiciones restantes: {summary['positions_remaining']}\n"
            f"Tiempo: {summary['elapsed_s']}s · Equity: {self.equity:,.2f} USDC\n"
            f"Bot BLOQUEADO — {self.risk.status_detail()}"
        )
        self.notifier.notify_text(msg, critical=True)
        await self._event("CRITICAL", "KILL_SWITCH", f"{reason} → {summary}")
        return f"OK: {summary}"

    async def _switch_mode(self, new_mode: str, src: str) -> str:
        if new_mode not in ("PAPER", "LIVE"):
            return "RECHAZADO: modo inválido"
        if new_mode == self.mode:
            return f"Sin cambios ({new_mode})"
        if self.execution.positions or self.execution.pending_products:
            return "RECHAZADO: cierra posiciones/entradas antes de cambiar de modo"
        if new_mode == "LIVE" and not (self.s.runtime.allow_live and self.s.creds.has_coinbase):
            return "RECHAZADO: LIVE requiere ALLOW_LIVE_TRADING=true y credenciales CDP"
        old_gateway = self.gateway
        if isinstance(old_gateway, PaperGateway):
            await self.db.run(self.db.sync.set_kv, "paper_balances", old_gateway.balances)
        self.mode = new_mode
        self.gateway = self._build_gateway(new_mode)
        await self._load_market_metadata()
        self.execution = self._build_execution()
        await self.execution.restore()
        if new_mode == "LIVE":
            await self._refresh_fee_rates()
            await asyncio.to_thread(self._start_user_ws)
        else:
            await asyncio.to_thread(self._stop_user_ws)
        await self.db.run(self.db.sync.set_kv, "mode_override", new_mode)
        await self._refresh_equity()
        self.risk.state.day_start_equity = self.equity
        self.risk.state.peak_equity = self.equity
        await self._persist_risk()
        self.notifier.notify_text(f"🔁 Modo cambiado a <b>{new_mode}</b> ({src})", critical=True)
        await self._event("WARN", "MODE", f"{src}: modo → {new_mode}")
        return f"OK: modo {new_mode}"

    # ═════════════════════════════════════════════════════════════════════
    # Riesgo / equity / watchdog
    # ═════════════════════════════════════════════════════════════════════
    async def _refresh_equity(self) -> None:
        try:
            self.balances = await self.gateway.get_balances()
        except Exception as exc:  # noqa: BLE001
            log.warning("get_balances falló: %s", exc)
            return
        quote = "USDC"
        cash = self.balances.get(quote, 0.0)
        eq = cash
        for pid, mk in self.markets.items():
            base = pid.split("-")[0]
            px = mk.book.mid or mk.last_price
            if px and not math.isnan(px):
                eq += self.balances.get(base, 0.0) * px
        self.cash, self.equity = cash, eq

    async def equity_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._refresh_equity()
            if self.equity > 0:
                unreal = sum(self.execution.unrealized_net(p, self.markets[p.product_id].book.best_bid or math.nan)
                             for p in self.execution.positions.values())
                await self.db.run(self.db.sync.insert_equity, time.time(), self.equity, self.cash, unreal, self.mode)
                evt = self.risk.on_equity(self.equity)
                if evt and evt.kind == "DAILY_DD":
                    await self._event("CRITICAL", "DAILY_DD", evt.message)
                    self.notifier.notify_risk(evt.message)
                    await self.kill_switch(evt.message, lock_reason="daily_dd")
            await self._persist_risk()
            if isinstance(self.gateway, PaperGateway):
                await self.db.run(self.db.sync.set_kv, "paper_balances", self.gateway.balances)
            await asyncio.sleep(self.s.runtime.equity_sample_s)

    async def _persist_risk(self) -> None:
        await self.db.run(self.db.sync.set_kv, "risk_state", self.risk.state.to_json())

    def _market_conns_ok(self) -> tuple[bool, float]:
        market = [c for c in self.conns.values() if not c.user]
        if not market:
            return False, float("inf")
        now = time.time()
        age = max(now - c.last_heartbeat for c in market)
        return all(c.connected for c in market), age

    async def watchdog_loop(self) -> None:
        last_ping = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if now - last_ping >= 5.0:
                last_ping = now
                try:
                    self.api_latency_ms = await asyncio.wait_for(self.gateway.ping(), timeout=5.0)
                except Exception:  # noqa: BLE001
                    self.api_latency_ms = 9_999.0
            connected, hb_age = self._market_conns_ok()
            evt = self.risk.watchdog(hb_age, self.api_latency_ms, connected)
            if evt:
                level = "CRITICAL" if evt.kind == "SAFE_ON" else "INFO"
                await self._event(level, evt.kind, evt.message)
                self.notifier.notify_risk(evt.message)
                if evt.kind == "SAFE_ON":
                    # cancelar entradas en curso: nunca perseguir precio con datos ciegos
                    for t in list(self.execution.entry_tasks.values()):
                        if not t.done():
                            t.cancel()
            await asyncio.sleep(1.0)

    async def reconcile_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(10.0)
            try:
                await self.execution.reconcile_open_orders()
                if self.risk.status is not BotStatus.KILLED:
                    await self.execution.ensure_tp_orders()
                elif self.execution.positions:
                    # Kill-switch con posiciones remanentes: reintento de liquidación
                    for pid in list(self.execution.positions):
                        await self.execution.close_position(pid, "KILL-SWITCH (reintento)")
                await self.db.run(self.db.sync.prune)
            except Exception:  # noqa: BLE001
                log.exception("Error en reconcile_loop")

    # ═════════════════════════════════════════════════════════════════════
    # Telemetría para el Dashboard
    # ═════════════════════════════════════════════════════════════════════
    async def snapshot_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._write_snapshot()
            except Exception:  # noqa: BLE001
                log.exception("Error escribiendo snapshot")
            await asyncio.sleep(self.s.runtime.snapshot_interval_s)

    async def _write_snapshot(self) -> None:
        rows = []
        for pid, a in self.strategy.analytics.items():
            p = self.s.pairs[pid]
            rows.append({
                "product_id": pid, "role": p.role.value, "price": _nn(a.price),
                "best_bid": _nn(a.best_bid), "best_ask": _nn(a.best_ask), "spread_pct": _nn(a.spread_pct),
                "max_spread_pct": p.max_spread_pct, "obi": _nn(a.obi), "atr": _nn(a.atr),
                "atr_pct": _nn(a.atr_pct), "regime": a.regime.value, "hurst": _nn(a.hurst),
                "adx": _nn(a.adx), "vwap": _nn(a.vwap), "cvd_5m": _nn(a.cvd_5m),
                "projected_move_pct": _nn(a.projected_move_pct), "fee_filter_ok": int(a.fee_ok),
                "fee_filter_reason": a.fee_reason, "signal": a.signal_state, "updated_at": a.ts,
            })
        o = self.strategy.oracle.state
        connected, hb_age = self._market_conns_ok()
        kv = {
            "bot_status": self.risk.status.value,
            "status_detail": self.risk.status_detail(),
            "mode": self.mode,
            "ws_latency_ms": round(self.ws_latency_ms, 1),
            "api_latency_ms": round(self.api_latency_ms, 1),
            "heartbeat_age_s": round(hb_age, 2) if hb_age != float("inf") else None,
            "ws_connected": connected,
            "oracle": {"veto": o.veto, "reason": o.reason, "change_5m_pct": o.change_5m_pct,
                       "cvd_ratio": o.cvd_ratio, "obi": o.obi, "since": o.since},
            "equity": self.equity,
            "cash": self.cash,
            "day_start_equity": self.risk.state.day_start_equity,
            "daily_dd_pct": self.risk.daily_drawdown_pct(self.equity) if self.equity else 0.0,
            "fees": {"maker": self.strategy.maker_fee, "taker": self.strategy.taker_fee},
            "engine_heartbeat": time.time(),
            "consecutive_losses": self.risk.state.consecutive_losses,
            "open_positions": len(self.execution.positions),
            "max_open_positions": self.s.risk.max_open_positions,
        }

        def write() -> None:
            self.db.sync.upsert_pair_snapshots(rows)
            self.db.sync.set_many_kv(kv)
            for r in self.execution.position_rows():
                self.db.sync.upsert_position(r)

        await self.db.run(write)

    async def _publish_kv(self, items: dict[str, Any]) -> None:
        await self.db.run(self.db.sync.set_many_kv, items)

    async def _event(self, level: str, category: str, message: str) -> None:
        log.log(logging.getLevelName(level) if level != "WARN" else logging.WARNING, "[%s] %s", category, message)
        self.db.fire(self.db.sync.log_event, level, category, message)

    # ═════════════════════════════════════════════════════════════════════
    # /status
    # ═════════════════════════════════════════════════════════════════════
    async def status_text(self) -> str:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        trades = await self.db.run(self.db.sync.trades, self.mode, day_start)
        realized = sum(t["net_pnl"] for t in trades)
        unreal = sum(self.execution.unrealized_net(p, self.markets[p.product_id].book.best_bid or math.nan)
                     for p in self.execution.positions.values())
        o = self.strategy.oracle.state
        lines = [
            f"📊 <b>STATUS</b> · {self.mode} · <b>{self.risk.status.value}</b>",
            f"<i>{self.risk.status_detail()}</i>",
            f"Equity: <b>{self.equity:,.2f} USDC</b> (caja {self.cash:,.2f})",
            f"PnL neto hoy: <b>{realized + unreal:+.2f}</b> (realizado {realized:+.2f} · flotante {unreal:+.2f})",
            f"Trades hoy: {len(trades)} · DD diario: {self.risk.daily_drawdown_pct(self.equity):.2f}%",
            f"Oráculo BTC: {'🛑 ' + o.reason if o.veto else '🟩 RISK-ON'}",
            f"Latencia WS {self.ws_latency_ms:.0f}ms · API {self.api_latency_ms:.0f}ms",
            "",
            "<b>Pares</b>",
        ]
        for pid, a in self.strategy.analytics.items():
            sp = f"{a.spread_pct:.3f}%" if not math.isnan(a.spread_pct) else "—"
            lines.append(
                f"• {pid}: {a.price:.6g} · {a.regime.value} · spread {sp} · OBI {a.obi:+.2f} · "
                f"{'✅' if a.fee_ok else '❌'} {a.projected_move_pct:.2f}%"
            )
        lines.append("")
        lines.append(f"<b>Posiciones abiertas ({len(self.execution.positions)})</b>")
        for p in self.execution.positions.values():
            bid = self.markets[p.product_id].book.best_bid or p.entry_price
            lines.append(
                f"• {p.product_id} {p.size:g} @ {p.entry_price:.6g} · SL {p.stop_price:.6g} · "
                f"neto {self.execution.unrealized_net(p, bid):+.2f} USDC"
            )
        if not self.execution.positions:
            lines.append("— ninguna —")
        return "\n".join(lines)

    # ═════════════════════════════════════════════════════════════════════
    # Parada
    # ═════════════════════════════════════════════════════════════════════
    async def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        log.warning("Apagando Core Engine…")
        for t in list(self.execution.entry_tasks.values()):
            if not t.done():
                t.cancel()
        await asyncio.gather(*self.execution.entry_tasks.values(), return_exceptions=True)
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self._persist_risk()
        if isinstance(self.gateway, PaperGateway):
            await self.db.run(self.db.sync.set_kv, "paper_balances", self.gateway.balances)
        await self._publish_kv({"bot_status": "STOPPED", "engine_heartbeat": time.time()})
        for c in self.conns.values():
            if c.client:
                try:
                    await asyncio.to_thread(c.client.close)
                except Exception:  # noqa: BLE001
                    pass
        self.notifier.notify_text(
            f"🔌 Core Engine detenido. Posiciones abiertas: {len(self.execution.positions)} "
            f"(TPs en reposo se mantienen; los stops software quedan inactivos).",
            critical=True,
        )
        await self.notifier.stop()
        await self.db.close()


def _nn(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-8s %(name)-10s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("coinbase").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def run() -> None:
    setup_logging(SETTINGS.runtime.log_level)
    engine = TradingEngine(SETTINGS)
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.shutdown()))
        except NotImplementedError:  # Windows
            pass
    try:
        await engine.start()
        await engine.stop_event.wait()
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
