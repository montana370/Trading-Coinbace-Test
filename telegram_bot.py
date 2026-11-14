"""
telegram_bot.py — Telemetría y control remoto vía Telegram (100 % asíncrono).

* Envío: `asyncio.Queue` + worker `aiohttp` con rate-limit y manejo de 429.
  `notify_*` son NO bloqueantes (put_nowait) → el motor nunca espera a la red.
* Recepción: long-polling `getUpdates`. Sólo se aceptan mensajes del
  `TELEGRAM_CHAT_ID` configurado. Los comandos de control se publican en el
  bus de comandos (SQLite) — mismo camino que el Dashboard — y PANIC se
  procesa con prioridad máxima.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from models import CommandType, Position, Signal

if TYPE_CHECKING:
    from execution_handler import EntryResult
    from risk_manager import SizingResult

log = logging.getLogger("telegram")

StatusProvider = Callable[[], Awaitable[str]]
CommandSink = Callable[[CommandType, str], Awaitable[None]]

API = "https://api.telegram.org/bot{token}/{method}"


def _e(x: Any) -> str:
    return html.escape(str(x))


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _px(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, min_interval_s: float = 1.05) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)
        self.min_interval_s = min_interval_s
        self.queue: asyncio.PriorityQueue[tuple[int, float, str]] = asyncio.PriorityQueue(maxsize=2000)
        self._session: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._offset = 0
        self.status_provider: StatusProvider | None = None
        self.command_sink: CommandSink | None = None
        self._stopping = False

    # ── ciclo de vida ───────────────────────────────────────────────────────
    async def start(self, status_provider: StatusProvider, command_sink: CommandSink) -> None:
        self.status_provider = status_provider
        self.command_sink = command_sink
        if not self.enabled:
            log.warning("Telegram deshabilitado (faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        self._tasks = [
            asyncio.create_task(self._sender(), name="tg-sender"),
            asyncio.create_task(self._poller(), name="tg-poller"),
        ]

    async def stop(self, drain_timeout: float = 5.0) -> None:
        self._stopping = True
        if self.enabled:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                pass
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session:
            await self._session.close()

    # ── API pública no bloqueante ───────────────────────────────────────────
    def send(self, text: str, critical: bool = False) -> None:
        log.info("TG » %s", text.replace("\n", " | ")[:300])
        if not self.enabled:
            return
        prio = 0 if critical else 1
        try:
            self.queue.put_nowait((prio, time.time(), text))
        except asyncio.QueueFull:
            log.error("Cola de Telegram llena; mensaje descartado")

    # ── worker de envío ─────────────────────────────────────────────────────
    async def _api(self, method: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        assert self._session is not None
        url = API.format(token=self.token, method=method)
        async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data: dict[str, Any] = await r.json(content_type=None)
            if r.status == 429:
                retry = float(data.get("parameters", {}).get("retry_after", 2))
                raise _RateLimited(retry)
            if not data.get("ok"):
                raise RuntimeError(f"Telegram {method}: {data.get('description')}")
            return data

    async def _sender(self) -> None:
        last = 0.0
        while True:
            _, _, text = await self.queue.get()
            try:
                wait = self.min_interval_s - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                for attempt in range(5):
                    try:
                        await self._api("sendMessage", {
                            "chat_id": self.chat_id, "text": text[:4000],
                            "parse_mode": "HTML", "disable_web_page_preview": True,
                        })
                        break
                    except _RateLimited as rl:
                        await asyncio.sleep(rl.retry_after + 0.5)
                    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                        log.warning("Envío Telegram falló (%d): %s", attempt + 1, exc)
                        await asyncio.sleep(min(2 ** attempt, 15))
                last = time.monotonic()
            finally:
                self.queue.task_done()

    # ── long polling de comandos ────────────────────────────────────────────
    async def _poller(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                data = await self._api(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25, "allowed_updates": ["message"]},
                    timeout=35.0,
                )
                backoff = 1.0
                for upd in data.get("result", []):
                    self._offset = max(self._offset, int(upd["update_id"]) + 1)
                    msg = upd.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if chat_id != self.chat_id:
                        log.warning("Comando ignorado de chat no autorizado %s", chat_id)
                        continue
                    if text.startswith("/"):
                        await self._handle(text)
            except asyncio.CancelledError:
                raise
            except _RateLimited as rl:
                await asyncio.sleep(rl.retry_after)
            except Exception as exc:  # noqa: BLE001
                log.warning("Polling Telegram: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle(self, text: str) -> None:
        cmd = text.split()[0].split("@")[0].lower()
        assert self.command_sink is not None
        if cmd == "/status" and self.status_provider:
            self.send(await self.status_provider(), critical=True)
        elif cmd == "/pause":
            await self.command_sink(CommandType.PAUSE, "telegram")
            self.send("⏸️ <b>PAUSE</b> recibido — congelando nuevas entradas…", critical=True)
        elif cmd == "/resume":
            await self.command_sink(CommandType.RESUME, "telegram")
            self.send("▶️ <b>RESUME</b> recibido — procesando…", critical=True)
        elif cmd == "/panic":
            await self.command_sink(CommandType.PANIC, "telegram")
            self.send("🚨 <b>PANIC KILL-SWITCH</b> recibido — cancelando órdenes y liquidando…", critical=True)
        else:
            self.send(
                "<b>Comandos</b>\n/status — resumen\n/pause — congelar entradas\n"
                "/resume — reanudar entradas\n/panic — Kill-Switch (cierra todo)",
                critical=True,
            )

    # ═════════════════════════════════════════════════════════════════════
    # Plantillas de alertas
    # ═════════════════════════════════════════════════════════════════════
    def notify_entry(
        self, pos: Position, sig: Signal, res: "EntryResult", sizing: "SizingResult", mode: str
    ) -> None:
        notional = pos.entry_price * pos.size
        est_exit_fee = pos.take_profit * pos.size * pos.entry_fee_rate
        tps = " / ".join(f"{_px(l.price)} ({l.fraction*100:.0f}%)" for l in pos.tp_levels)
        self.send(
            f"🟢 <b>ENTRADA EJECUTADA</b> [{_e(mode)}]\n"
            f"<b>{_e(pos.product_id)}</b> LONG · {_e(sig.strategy.value)}\n"
            f"Llenado: <code>{_px(pos.entry_price)}</code> · {'MAKER (Post-Only)' if res.maker else 'TAKER'}"
            f" · {res.attempts} intento(s)\n"
            f"Tamaño: <code>{pos.size:g}</code> ≈ <b>{notional:,.2f} USDC</b>\n"
            f"Stop Loss: <code>{_px(pos.stop_price)}</code> · TP final: <code>{_px(pos.take_profit)}</code>\n"
            f"TPs escalonados: {tps}\n"
            f"OBI L2: <b>{sig.obi:+.2f}</b> · Régimen: <b>{_e(sig.regime.value)}</b>\n"
            f"Riesgo: {sizing.risk_usd:,.2f} USDC ({_e(sizing.method)}) · RR neto {sizing.net_rr:.2f}\n"
            f"Comisión entrada: {pos.entry_fee_paid:.4f} USDC · salida est.: {est_exit_fee:.4f} USDC\n"
            f"Recorrido proyectado: {sig.projected_move_pct:.2f}%\n"
            f"<i>{_e(sig.reason)}</i>"
        )

    def notify_breakeven(self, pos: Position, be_price: float, mode: str) -> None:
        self.send(
            f"🛡️ <b>FEE-ADJUSTED BREAKEVEN</b> [{_e(mode)}]\n"
            f"<b>{_e(pos.product_id)}</b> alcanzó +{self_r(pos):.2f}R\n"
            f"Stop → <code>{_px(be_price)}</code> (entrada {_px(pos.entry_price)} + comisiones ida/vuelta)\n"
            f"Pérdida neta máxima ahora: 0.00 USDC"
        )

    def notify_partial_tp(self, pos: Position, price: float, qty: float, fee: float, mode: str) -> None:
        gross = (price - pos.entry_price) * qty
        self.send(
            f"🎯 <b>TAKE PROFIT PARCIAL</b> [{_e(mode)}]\n"
            f"<b>{_e(pos.product_id)}</b> vendido {qty:g} @ <code>{_px(price)}</code> (MAKER)\n"
            f"PnL bruto tramo: {gross:+.2f} USDC · comisión {fee:.4f} USDC\n"
            f"Restante: {pos.size:g} · Stop: <code>{_px(pos.stop_price)}</code>"
        )

    def notify_close(self, pos: Position, trade: dict[str, Any], mode: str) -> None:
        net = trade["net_pnl"]
        icon = "✅" if net >= 0 else "🔴"
        self.send(
            f"{icon} <b>POSICIÓN CERRADA</b> [{_e(mode)}] — {_e(trade['exit_reason'])}\n"
            f"<b>{_e(pos.product_id)}</b> · {_e(pos.strategy.value)}\n"
            f"Entrada <code>{_px(trade['entry_price'])}</code> → Salida media <code>{_px(trade['exit_price'])}</code>\n"
            f"PnL bruto: {trade['gross_pnl']:+.2f} USDC\n"
            f"Comisiones Coinbase: −{trade['fees']:.4f} USDC\n"
            f"<b>PnL NETO: {net:+.2f} USDC ({trade['net_pct']:+.2f}%)</b>\n"
            f"Duración: {_fmt_dur(trade['duration_s'])}",
            critical=True,
        )

    def notify_oracle(self, veto: bool, reason: str) -> None:
        if veto:
            self.send(f"🛑 <b>ORÁCULO BTC — VETO ACTIVO</b>\nCompras en SOL/ETH/SUI bloqueadas\n{_e(reason)}",
                      critical=True)
        else:
            self.send("🟩 <b>ORÁCULO BTC — RISK-ON</b>\nVeto bajista levantado", critical=True)

    def notify_risk(self, text: str) -> None:
        self.send(f"⚠️ <b>RIESGO</b>\n{_e(text)}", critical=True)

    def notify_text(self, text: str, critical: bool = False) -> None:
        self.send(text, critical=critical)


def self_r(pos: Position) -> float:
    return pos.r_multiple(pos.highest_price)


class _RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after
