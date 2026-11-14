"""
config.py — Configuración central de la plataforma.

Todos los parámetros se pueden sobrescribir mediante variables de entorno
(archivo `.env`). Las credenciales NUNCA se escriben en código.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

try:  # python-dotenv es opcional en tiempo de ejecución
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class PairRole(str, Enum):
    ALPHA_ENGINE = "Motor Principal de Alfa"
    LIQUIDITY_ANCHOR = "Ancla de Liquidez y Oráculo Macro"
    MEAN_REVERSION = "Especialista Mean-Reversion"
    MOMENTUM = "Captura de Impulso / Volatilidad"


@dataclass(frozen=True)
class PairConfig:
    product_id: str
    role: PairRole
    max_allocation: float          # fracción del equity (0.35 = 35 %)
    max_spread_pct: float          # spread máximo permitido en % (0.04 = 0.04 %)
    allow_trend: bool              # Estrategia 1 habilitada
    allow_mean_reversion: bool     # Estrategia 2 habilitada

    @property
    def base_currency(self) -> str:
        return self.product_id.split("-")[0]

    @property
    def quote_currency(self) -> str:
        return self.product_id.split("-")[1]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Universo precalibrado
# ─────────────────────────────────────────────────────────────────────────────
ORACLE_PRODUCT = "BTC-USDC"

PAIRS: dict[str, PairConfig] = {
    "SOL-USDC": PairConfig("SOL-USDC", PairRole.ALPHA_ENGINE, 0.35, 0.04, True, True),
    "BTC-USDC": PairConfig("BTC-USDC", PairRole.LIQUIDITY_ANCHOR, 0.30, 0.02, True, True),
    "ETH-USDC": PairConfig("ETH-USDC", PairRole.MEAN_REVERSION, 0.20, 0.03, False, True),
    "SUI-USDC": PairConfig("SUI-USDC", PairRole.MOMENTUM, 0.15, 0.06, True, False),
}
PRODUCT_IDS: list[str] = list(PAIRS.keys())
QUOTE_CURRENCY = "USDC"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Credenciales (CDP keys — JWT ES256)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Credentials:
    # Formato CDP: "organizations/{org_id}/apiKeys/{key_id}"
    coinbase_api_key: str = _env_str("COINBASE_API_KEY", "")
    # PEM EC PRIVATE KEY. En .env se puede escribir con "\n" literales.
    coinbase_api_secret: str = _env_str("COINBASE_API_SECRET", "").replace("\\n", "\n")
    telegram_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")

    @property
    def has_coinbase(self) -> bool:
        return bool(self.coinbase_api_key and self.coinbase_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Comisiones y ejecución
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FeeConfig:
    maker_fee: float = _env_float("MAKER_FEE", 0.0040)      # 0.40 %
    taker_fee: float = _env_float("TAKER_FEE", 0.0060)      # 0.60 %
    # Si True, el motor consulta get_transaction_summary() y usa el tier real
    # (nunca por debajo de los valores configurados: modelo conservador).
    fetch_live_fee_tier: bool = _env_bool("FETCH_LIVE_FEE_TIER", True)
    min_projected_move_pct: float = _env_float("MIN_PROJECTED_MOVE_PCT", 1.25)   # %
    min_cost_multiple: float = _env_float("MIN_COST_MULTIPLE", 3.0)              # x


@dataclass(frozen=True)
class ExecutionConfig:
    chase_interval_s: float = _env_float("CHASE_INTERVAL_S", 4.0)
    max_chase_attempts: int = _env_int("MAX_CHASE_ATTEMPTS", 6)
    max_chase_slippage_pct: float = _env_float("MAX_CHASE_SLIPPAGE_PCT", 0.15)    # %
    min_net_rr: float = _env_float("MIN_NET_RR", 1.8)
    order_prefix: str = _env_str("ORDER_PREFIX", "qtx")
    exit_slippage_guard_pct: float = _env_float("EXIT_SLIPPAGE_GUARD_PCT", 0.50)  # paper
    # En el canal market_trades de Coinbase, `side` indica el lado del agresor
    # (taker). Si tu verificación empírica indica lo contrario, pon False.
    trade_side_is_taker: bool = _env_bool("TRADE_SIDE_IS_TAKER", True)
    # Stops de respaldo en el exchange: los TPs se envían como órdenes bracket
    # (límite TP + disparo de stop). El stop del exchange queda un poco por
    # debajo del stop software para actuar sólo si el motor no responde.
    exchange_stops: bool = _env_bool("EXCHANGE_STOPS", True)
    exchange_stop_buffer_pct: float = _env_float("EXCHANGE_STOP_BUFFER_PCT", 0.20)
    exchange_stop_min_update_s: float = _env_float("EXCHANGE_STOP_MIN_UPDATE_S", 20.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Estrategia / Régimen / Microestructura
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StrategyConfig:
    bar_seconds: int = 60                    # barras base de 1 minuto
    regime_tf_minutes: int = 5               # régimen calculado en barras de 5m
    history_bars_1m: int = 1500              # mínimo; se amplía según la TF de señal (ver required_history_1m)
    eval_interval_s: float = _env_float("EVAL_INTERVAL_S", 1.0)

    # Régimen
    hurst_window: int = 120
    adx_period: int = 14
    atr_period: int = 14
    vol_percentile_window: int = 288         # 24 h de barras de 5m
    hurst_trend: float = 0.55
    hurst_mr: float = 0.45
    adx_trend: float = 25.0
    adx_range: float = 20.0
    vol_pct_noise_high: float = 0.97         # percentil de ATR% por encima → ruido
    vol_pct_noise_low: float = 0.03          # mercado "muerto" → ruido

    # Estrategia 1 — Breakout
    bb_period: int = 20
    bb_std: float = 2.0
    kc_mult: float = 1.5
    squeeze_lookback: int = 6                # barras de la TF de señal desde la liberación
    breakout_require_squeeze: bool = True    # exigir liberación de squeeze (False = ruptura BB simple)
    volume_confirm_mult: float = 1.5
    cvd_window_s: int = 300

    # Estrategia 2 — Mean-Reversion
    vwap_sigma_entry: float = 2.5
    rsi_period: int = 14
    rsi_1m_oversold: float = 25.0
    rsi_5m_oversold: float = 35.0
    absorption_window_s: int = 60
    absorption_sell_buy_ratio: float = 1.5
    absorption_max_range_atr: float = 0.35
    mr_require_absorption: bool = True       # exigir absorción en el flujo de órdenes

    # OBI
    obi_levels: int = 10                     # mínimo de niveles por lado
    obi_band_bps: float = _env_float("OBI_BAND_BPS", 10.0)   # banda ±0.10 % del mid
    obi_min_long: float = 0.10               # OBI mínimo para entrar en largo
    wall_multiple: float = 4.0               # nivel > 4x mediana = muro

    # Salidas (múltiplos de ATR del marco de régimen — 5m)
    sl_atr_mult: float = 2.0
    tp_atr_mult_trend: float = 6.0
    tp_atr_mult_mr: float = 4.0
    trail_atr_mult: float = 2.5
    breakeven_trigger_r: float = 1.2
    partial_tp_fractions: tuple[float, ...] = (0.40, 0.30)   # resto → trailing
    partial_tp_r: tuple[float, ...] = (1.6, 2.6)


def required_history_1m(c: "StrategyConfig") -> int:
    """Velas de 1m necesarias para calentar Hurst + percentil de volatilidad en la TF de señal."""
    return max(c.history_bars_1m, (c.hurst_window + c.vol_percentile_window + 40) * c.regime_tf_minutes)


@dataclass(frozen=True)
class TrendConfig:
    """
    Seguimiento de tendencia en velas de 6 h (validado en docs/BACKTEST.md):
    entrada si el cierre supera el máximo de `n_entry` velas y la EMA(200);
    stop inicial y trailing a `k_atr`·ATR(14) desde el máximo; salida si el
    cierre cae bajo el mínimo de `n_exit` velas. Sin TP fijo.
    """
    tf_seconds: int = 21600
    granularity: str = "SIX_HOUR"
    n_entry: int = _env_int("TREND_N_ENTRY", 20)
    n_exit: int = _env_int("TREND_N_EXIT", 20)
    k_atr: float = _env_float("TREND_K_ATR", 5.0)
    ema_period: int = 200
    atr_period: int = 14
    history_bars: int = 300                  # 75 días de velas de 6 h
    entry_window_s: int = 1800               # la señal sólo es válida 30 min tras el cierre
    far_tp_multiple: float = 4.0             # TP del bracket (sólo para alojar el stop en el exchange)


@dataclass(frozen=True)
class OracleConfig:
    window_s: int = 300
    max_drop_pct: float = _env_float("ORACLE_MAX_DROP_PCT", 0.60)    # caída 5m %
    cvd_veto_ratio: float = _env_float("ORACLE_CVD_VETO", -0.35)     # CVD/volumen
    obi_veto: float = _env_float("ORACLE_OBI_VETO", -0.35)
    release_cooldown_s: int = 180


# ─────────────────────────────────────────────────────────────────────────────
# 5. Riesgo
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = _env_float("RISK_PER_TRADE", 0.01)       # 1 % equity
    use_kelly: bool = _env_bool("USE_KELLY", True)
    kelly_fraction: float = 0.25                                     # Quarter-Kelly
    kelly_min_trades: int = 30
    kelly_cap: float = 0.02                                          # máx 2 % riesgo
    max_open_positions: int = _env_int("MAX_OPEN_POSITIONS", 2)
    max_daily_drawdown: float = _env_float("MAX_DAILY_DRAWDOWN", 0.03)
    daily_dd_lock_hours: float = 24.0
    max_consecutive_losses: int = 3
    loss_streak_pause_hours: float = 6.0
    heartbeat_timeout_s: float = _env_float("HEARTBEAT_TIMEOUT_S", 5.0)
    max_api_latency_ms: float = _env_float("MAX_API_LATENCY_MS", 500.0)
    latency_breach_count: int = 3            # brechas consecutivas → SAFE
    min_order_usd: float = 10.0


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str = _env_str("TRADING_MODE", "PAPER").upper()            # PAPER | LIVE
    allow_live: bool = _env_bool("ALLOW_LIVE_TRADING", False)        # interlock
    paper_starting_equity: float = _env_float("PAPER_STARTING_EQUITY", 10_000.0)
    db_path: Path = Path(_env_str("DB_PATH", "data/trading_state.db"))
    log_level: str = _env_str("LOG_LEVEL", "INFO")
    snapshot_interval_s: float = 0.5
    command_poll_s: float = 0.25
    equity_sample_s: float = 15.0


@dataclass(frozen=True)
class Settings:
    creds: Credentials = field(default_factory=Credentials)
    fees: FeeConfig = field(default_factory=FeeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    # Estrategias activas (coma): TREND_6H | BREAKOUT_ORDERFLOW | MEAN_REVERSION_MICRO
    strategies: tuple[str, ...] = tuple(
        x.strip().upper() for x in _env_str("STRATEGIES", "TREND_6H").split(",") if x.strip()
    )
    risk: RiskConfig = field(default_factory=RiskConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    pairs: dict[str, PairConfig] = field(default_factory=lambda: dict(PAIRS))


SETTINGS = Settings()
