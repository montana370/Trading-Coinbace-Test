"""
risk_manager.py — Gestión de riesgo y capital con poder de VETO total.

Responsabilidades:
* Máquina de estados del bot (RUNNING / PAUSED / SAFE / KILLED).
* Dimensionamiento: riesgo fijo 1 % o Quarter-Kelly sobre la distancia al
  stop (incluyendo comisiones), con tope por asignación del par y caja libre.
* Ratio Riesgo/Beneficio NETO de comisiones.
* Fee-Adjusted Breakeven exacto.
* Circuit breakers: Daily Drawdown 3 %, racha de 3 pérdidas, watchdog de
  heartbeats/latencia.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import PairConfig, RiskConfig
from exchange import ProductMeta, round_step
from models import BotStatus, Signal

log = logging.getLogger("risk")


@dataclass
class RiskState:
    manual_paused: bool = False
    panic_killed: bool = False
    dd_lock_until: float = 0.0
    loss_pause_until: float = 0.0
    safe_mode: bool = False
    safe_reason: str = ""
    consecutive_losses: int = 0
    day: int = -1
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    latency_breaches: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "RiskState":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class SizingResult:
    base_size: float
    notional: float
    risk_usd: float
    risk_fraction: float
    method: str
    net_rr: float
    reason: str = ""
    ok: bool = True


@dataclass
class RiskEvent:
    kind: str           # DAILY_DD | LOSS_STREAK | SAFE_ON | SAFE_OFF
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def fee_adjusted_breakeven(entry: float, entry_fee_rate: float, exit_fee_rate: float, short: bool = False) -> float:
    """
    Precio de salida con PnL neto = 0:
        largo:  exit·(1 − f_exit) = entry·(1 + f_entry)  ⇒  exit = entry·(1+f_entry)/(1−f_exit)
        corto:  entry·(1 − f_entry) = exit·(1 + f_exit)  ⇒  exit = entry·(1−f_entry)/(1+f_exit)
    """
    if short:
        return entry * (1.0 - entry_fee_rate) / (1.0 + exit_fee_rate)
    return entry * (1.0 + entry_fee_rate) / (1.0 - exit_fee_rate)


def net_reward_risk(entry: float, stop: float, tp: float, maker: float, taker: float) -> tuple[float, float, float]:
    """(recompensa neta/unidad, riesgo neto/unidad, RR neto). TP como maker, stop como taker.
    El sentido se deduce del stop: stop < entrada ⇒ largo; stop > entrada ⇒ corto."""
    d = 1.0 if stop < entry else -1.0
    reward = (tp - entry) * d - entry * maker - tp * maker
    risk = (entry - stop) * d + entry * maker + stop * taker
    return reward, risk, (reward / risk if risk > 0 else 0.0)


class RiskManager:
    def __init__(self, cfg: RiskConfig, pairs: dict[str, PairConfig], min_net_rr: float) -> None:
        self.cfg = cfg
        self.pairs = pairs
        self.min_net_rr = min_net_rr
        self.state = RiskState()

    # ── estado efectivo ─────────────────────────────────────────────────────
    @property
    def status(self) -> BotStatus:
        now = time.time()
        if self.state.panic_killed or now < self.state.dd_lock_until:
            return BotStatus.KILLED
        if self.state.safe_mode:
            return BotStatus.SAFE
        if self.state.manual_paused or now < self.state.loss_pause_until:
            return BotStatus.PAUSED
        return BotStatus.RUNNING

    def status_detail(self) -> str:
        now = time.time()
        s = self.state
        if s.panic_killed:
            return "Kill-Switch activado (requiere /resume)"
        if now < s.dd_lock_until:
            return f"Daily DD lock hasta {_fmt_ts(s.dd_lock_until)}"
        if s.safe_mode:
            return f"SAFE: {s.safe_reason}"
        if s.manual_paused:
            return "Pausa manual"
        if now < s.loss_pause_until:
            return f"Racha de pérdidas: pausa hasta {_fmt_ts(s.loss_pause_until)}"
        return "Operativo"

    @property
    def entries_allowed(self) -> bool:
        return self.status is BotStatus.RUNNING

    # ── comandos ────────────────────────────────────────────────────────────
    def pause(self) -> None:
        self.state.manual_paused = True

    def resume(self) -> tuple[bool, str]:
        now = time.time()
        if now < self.state.dd_lock_until:
            return False, f"Bloqueo por Daily Drawdown activo hasta {_fmt_ts(self.state.dd_lock_until)}"
        self.state.manual_paused = False
        self.state.panic_killed = False
        return True, "Entradas reanudadas"

    def panic(self) -> None:
        self.state.panic_killed = True
        self.state.manual_paused = True

    # ── equity / drawdown diario ────────────────────────────────────────────
    def on_equity(self, equity: float) -> RiskEvent | None:
        s = self.state
        today = int(datetime.now(timezone.utc).timestamp() // 86400)
        if s.day != today or s.day_start_equity <= 0:
            s.day = today
            s.day_start_equity = equity
        s.peak_equity = max(s.peak_equity, equity)
        dd = (s.day_start_equity - equity) / s.day_start_equity if s.day_start_equity > 0 else 0.0
        if dd >= self.cfg.max_daily_drawdown and time.time() >= s.dd_lock_until:
            s.dd_lock_until = time.time() + self.cfg.daily_dd_lock_hours * 3600
            return RiskEvent(
                "DAILY_DD",
                f"Daily Drawdown {dd*100:.2f}% ≥ {self.cfg.max_daily_drawdown*100:.1f}% — "
                f"flatten y bloqueo {self.cfg.daily_dd_lock_hours:.0f}h",
                {"drawdown_pct": dd * 100, "equity": equity, "day_start": s.day_start_equity},
            )
        return None

    def daily_drawdown_pct(self, equity: float) -> float:
        s = self.state
        if s.day_start_equity <= 0:
            return 0.0
        return max(0.0, (s.day_start_equity - equity) / s.day_start_equity * 100.0)

    # ── racha de pérdidas ───────────────────────────────────────────────────
    def on_trade_closed(self, net_pnl: float) -> RiskEvent | None:
        s = self.state
        if net_pnl < 0:
            s.consecutive_losses += 1
        else:
            s.consecutive_losses = 0
        if s.consecutive_losses >= self.cfg.max_consecutive_losses:
            s.loss_pause_until = time.time() + self.cfg.loss_streak_pause_hours * 3600
            s.consecutive_losses = 0
            return RiskEvent(
                "LOSS_STREAK",
                f"{self.cfg.max_consecutive_losses} pérdidas consecutivas — pausa "
                f"{self.cfg.loss_streak_pause_hours:.0f}h hasta {_fmt_ts(s.loss_pause_until)}",
            )
        return None

    # ── watchdog ────────────────────────────────────────────────────────────
    def watchdog(self, heartbeat_age_s: float, api_latency_ms: float, ws_connected: bool) -> RiskEvent | None:
        s = self.state
        reasons: list[str] = []
        if not ws_connected:
            reasons.append("WebSocket desconectado")
        if heartbeat_age_s > self.cfg.heartbeat_timeout_s:
            reasons.append(f"sin heartbeats {heartbeat_age_s:.1f}s")
        if api_latency_ms > self.cfg.max_api_latency_ms:
            s.latency_breaches += 1
        else:
            s.latency_breaches = 0
        if s.latency_breaches >= self.cfg.latency_breach_count:
            reasons.append(f"latencia API {api_latency_ms:.0f}ms > {self.cfg.max_api_latency_ms:.0f}ms")
        if reasons and not s.safe_mode:
            s.safe_mode, s.safe_reason = True, "; ".join(reasons)
            return RiskEvent("SAFE_ON", f"MODO SEGURO: {s.safe_reason}")
        if reasons:
            s.safe_reason = "; ".join(reasons)
        if not reasons and s.safe_mode:
            s.safe_mode, s.safe_reason = False, ""
            return RiskEvent("SAFE_OFF", "Conectividad restablecida — modo seguro desactivado")
        return None

    # ── veto de apertura ────────────────────────────────────────────────────
    def can_open(self, product_id: str, open_products: set[str], pending_products: set[str]) -> tuple[bool, str]:
        if not self.entries_allowed:
            return False, f"Estado {self.status.value}: {self.status_detail()}"
        if product_id in open_products or product_id in pending_products:
            return False, "Ya existe posición/entrada en el par"
        if len(open_products | pending_products) >= self.cfg.max_open_positions:
            return False, f"Máx. posiciones simultáneas ({self.cfg.max_open_positions})"
        return True, "OK"

    # ── Kelly ───────────────────────────────────────────────────────────────
    def kelly_fraction(self, trades: list[dict[str, Any]]) -> float | None:
        if not self.cfg.use_kelly or len(trades) < self.cfg.kelly_min_trades:
            return None
        recent = trades[-200:]
        wins = [t["net_pnl"] for t in recent if t["net_pnl"] > 0]
        losses = [-t["net_pnl"] for t in recent if t["net_pnl"] < 0]
        if not wins or not losses:
            return None
        w = len(wins) / len(recent)
        r = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        f = w - (1 - w) / r
        return f * self.cfg.kelly_fraction

    # ── sizing ──────────────────────────────────────────────────────────────
    def size_position(
        self,
        signal: Signal,
        entry_price: float,
        equity: float,
        free_quote: float,
        maker: float,
        taker: float,
        meta: ProductMeta,
        trades: list[dict[str, Any]],
    ) -> SizingResult:
        pcfg = self.pairs[signal.product_id]
        reward_u, risk_u, rr = net_reward_risk(entry_price, signal.stop_price, signal.take_profit, maker, taker)
        short = signal.side.value == "SELL"
        if risk_u <= 0 or (entry_price <= signal.stop_price if not short else entry_price >= signal.stop_price):
            return SizingResult(0, 0, 0, 0, "-", rr, "Stop inválido", ok=False)
        if rr < self.min_net_rr:
            return SizingResult(0, 0, 0, 0, "-", rr, f"RR neto {rr:.2f} < {self.min_net_rr:.2f}", ok=False)

        qk = self.kelly_fraction(trades)
        if qk is None:
            risk_frac, method = self.cfg.risk_per_trade, "FIXED_1%"
        else:
            risk_frac = min(max(qk, 0.0025), self.cfg.kelly_cap)
            method = f"QUARTER_KELLY({qk*100:.2f}%)"
        risk_usd = equity * risk_frac
        size = risk_usd / risk_u
        cap_alloc = pcfg.max_allocation * equity * max(self.cfg.max_leverage, 1.0) / entry_price
        cap_cash = max(free_quote * 0.98, 0.0) / (entry_price * (1 + maker))
        capped_by = ""
        if size > cap_alloc:
            size, capped_by = cap_alloc, f" (tope asignación {pcfg.max_allocation*100:.0f}%)"
        if size > cap_cash:
            size, capped_by = cap_cash, " (tope caja libre)"
        size = round_step(size, meta.base_increment)
        notional = size * entry_price
        if size < meta.base_min_size or notional < max(self.cfg.min_order_usd, meta.quote_min_size):
            return SizingResult(0, notional, risk_usd, risk_frac, method, rr,
                                f"Tamaño {notional:.2f} USDC por debajo del mínimo", ok=False)
        return SizingResult(size, notional, size * risk_u, risk_frac, method + capped_by, rr)

    def check_entry_still_valid(self, entry: float, stop: float, tp: float, maker: float, taker: float) -> tuple[bool, float]:
        _, _, rr = net_reward_risk(entry, stop, tp, maker, taker)
        return rr >= self.min_net_rr, rr


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
