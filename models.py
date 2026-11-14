"""
models.py — Tipos de dominio compartidos por todos los módulos.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class BotStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"          # sin nuevas entradas, gestiona posiciones
    SAFE = "SAFE"              # watchdog: sin nuevas entradas (auto-recuperable)
    KILLED = "KILLED"          # kill-switch / drawdown: todo cerrado y bloqueado


class Regime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    NOISE = "NOISE"
    WARMUP = "WARMUP"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyName(str, Enum):
    BREAKOUT = "BREAKOUT_ORDERFLOW"
    MEAN_REVERSION = "MEAN_REVERSION_MICRO"
    TREND = "TREND_6H"


class CommandType(str, Enum):
    PANIC = "PANIC"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    SET_MODE = "SET_MODE"
    RELOAD_CREDENTIALS = "RELOAD_CREDENTIALS"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
            OrderStatus.REJECTED,
        }


@dataclass
class OrderUpdate:
    """Evento normalizado de estado de orden (canal `user` o simulador paper)."""

    order_id: str
    client_order_id: str
    product_id: str
    side: Side
    status: OrderStatus
    filled_size: float
    avg_price: float
    total_fees: float
    leaves_size: float
    limit_price: float = 0.0
    ts: float = field(default_factory=time.time)


@dataclass
class Signal:
    product_id: str
    strategy: StrategyName
    side: Side
    regime: Regime
    ref_price: float
    stop_price: float
    take_profit: float
    atr: float
    obi: float
    projected_move_pct: float
    reason: str
    ts: float = field(default_factory=time.time)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.ref_price - self.stop_price)


@dataclass
class TPLevel:
    price: float
    fraction: float
    order_id: str | None = None
    client_order_id: str | None = None
    filled: bool = False


@dataclass
class Position:
    position_id: str
    product_id: str
    strategy: StrategyName
    regime: Regime
    entry_price: float
    size: float                     # tamaño base vivo
    initial_size: float
    stop_price: float
    initial_stop: float
    take_profit: float
    atr_at_entry: float
    entry_fee_paid: float           # USDC pagados al entrar
    entry_fee_rate: float
    opened_at: float
    obi_at_entry: float
    tp_levels: list[TPLevel] = field(default_factory=list)
    breakeven_armed: bool = False
    highest_price: float = 0.0
    realized_gross: float = 0.0     # PnL bruto de parciales ya cerradas
    exit_fees_paid: float = 0.0
    closing: bool = False

    @property
    def r_value(self) -> float:
        """Riesgo inicial por unidad (1R)."""
        return max(self.entry_price - self.initial_stop, 1e-12)

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / self.r_value

    def notional(self, price: float) -> float:
        return self.size * price
