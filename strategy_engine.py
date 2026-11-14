"""
strategy_engine.py — Inteligencia de señales.

Pipeline de evaluación por par (cada `eval_interval_s`):

    datos listos → spread ≤ máx → régimen (Hurst/ADX/ATR%) ≠ NOISE
      → estrategia según régimen (Breakout+OrderFlow | MeanReversion+Micro)
      → filtro OBI L2 → veto Oráculo BTC → Fee-Hurdle Filter → Signal

El RiskManager aplica después su veto final (estado, límites, sizing).

NOTA DE DISEÑO: Coinbase Advanced Trade spot no permite ventas en corto sin
margen; el motor es LONG-only. Las condiciones bajistas se usan como vetos
(Oráculo BTC, OBI negativo) y no como entradas cortas.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

import indicators as ind
from config import ORACLE_PRODUCT, FeeConfig, OracleConfig, PairConfig, StrategyConfig, TrendConfig
from market_data import MarketState
from models import Regime, Side, Signal, StrategyName


# ─────────────────────────────────────────────────────────────────────────────
# Analítica por par
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PairAnalytics:
    product_id: str
    price: float = float("nan")
    best_bid: float = float("nan")
    best_ask: float = float("nan")
    spread_pct: float = float("nan")
    obi: float = 0.0
    regime: Regime = Regime.WARMUP
    hurst: float = float("nan")
    adx: float = float("nan")
    atr: float = float("nan")          # ATR(14) en barras de 5m
    atr_pct: float = float("nan")
    vol_percentile: float = float("nan")
    vwap: float = float("nan")
    vwap_sigma: float = float("nan")
    cvd_5m: float = 0.0
    cvd_ratio_5m: float = 0.0
    rsi_1m: float = float("nan")
    rsi_5m: float = float("nan")
    projected_move_pct: float = float("nan")
    fee_ok: bool = False
    fee_reason: str = "warmup"
    trend_entry_level: float = float("nan")    # máximo de N velas 6 h (ruptura)
    trend_ema: float = float("nan")            # EMA 200 en 6 h
    trend_exit_level: float = float("nan")     # mínimo de M velas 6 h (salida de canal)
    trend_atr: float = float("nan")            # ATR(14) en 6 h
    signal_state: str = "WARMUP"
    ts: float = field(default_factory=time.time)


@dataclass
class FeeHurdleResult:
    ok: bool
    projected_pct: float
    round_trip_cost_pct: float
    required_pct: float
    reason: str


def fee_hurdle(
    entry: float, take_profit: float, spread_pct: float, maker_fee: float, cfg: FeeConfig
) -> FeeHurdleResult:
    """
    Filtro de Rentabilidad Mínima Neta (Anti-Fee Bleed).
    Exige: recorrido ≥ MIN_PROJECTED_MOVE_PCT  Y  recorrido ≥ k × (2·maker + spread).
    """
    projected = (take_profit - entry) / entry * 100.0 if entry > 0 else 0.0
    rt_cost = 2.0 * maker_fee * 100.0 + max(spread_pct, 0.0)
    required = max(cfg.min_projected_move_pct, cfg.min_cost_multiple * rt_cost)
    ok = projected >= required
    reason = (
        f"OK {projected:.2f}% ≥ {required:.2f}%"
        if ok
        else f"Recorrido {projected:.2f}% < requerido {required:.2f}% (coste RT {rt_cost:.2f}%)"
    )
    return FeeHurdleResult(ok, projected, rt_cost, required, reason)


# ─────────────────────────────────────────────────────────────────────────────
# Detector de régimen
# ─────────────────────────────────────────────────────────────────────────────
class RegimeDetector:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    def classify(self, s5: dict[str, np.ndarray]) -> tuple[Regime, dict[str, float]]:
        c = self.cfg
        close, high, low = s5["close"], s5["high"], s5["low"]
        need = max(c.hurst_window, 2 * c.adx_period + 2, c.bb_period + c.squeeze_lookback + 2)
        if len(close) < need:
            return Regime.WARMUP, {}
        atr = ind.atr(high, low, close, c.atr_period)
        atr_pct_series = atr / close * 100.0
        adx = ind.adx(high, low, close, c.adx_period)
        hurst = ind.hurst_exponent(close[-c.hurst_window:], max_lag=20)
        window = atr_pct_series[-c.vol_percentile_window:]
        vol_pctile = ind.percentile_rank(window, atr_pct_series[-1])
        m = {
            "atr": float(atr[-1]),
            "atr_pct": float(atr_pct_series[-1]),
            "adx": float(adx[-1]),
            "hurst": hurst,
            "vol_percentile": vol_pctile,
        }
        if any(math.isnan(v) for v in m.values()):
            return Regime.WARMUP, m
        # (C) Incertidumbre: volatilidad extrema (shock) o mercado muerto
        if vol_pctile >= c.vol_pct_noise_high or vol_pctile <= c.vol_pct_noise_low:
            return Regime.NOISE, m
        # (A) Tendencia fuerte: persistencia + fuerza direccional
        if hurst >= c.hurst_trend and m["adx"] >= c.adx_trend:
            return Regime.TREND, m
        # (B) Rango / mean-reversion: anti-persistencia + ADX bajo
        if hurst <= c.hurst_mr and m["adx"] <= c.adx_range:
            return Regime.RANGE, m
        return Regime.NOISE, m


# ─────────────────────────────────────────────────────────────────────────────
# Seguimiento de tendencia 6 h
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TrendState:
    ready: bool = False
    bar_start: float = 0.0          # inicio de la última vela 6 h CERRADA evaluada
    bar_close_ts: float = 0.0
    close: float = float("nan")
    entry_level: float = float("nan")
    exit_level: float = float("nan")
    ema: float = float("nan")
    atr: float = float("nan")
    breakout: bool = False
    channel_exit: bool = False


class TrendStrategy:
    """Mismas reglas que research_trend.simulate (validado fuera de muestra)."""

    def __init__(self, cfg: TrendConfig) -> None:
        self.cfg = cfg
        self.state: dict[str, TrendState] = {}

    @staticmethod
    def compute(s: dict[str, np.ndarray], cfg: TrendConfig) -> TrendState:
        c, h, l = s["close"], s["high"], s["low"]
        st = TrendState()
        need = max(cfg.ema_period, cfg.n_entry, cfg.n_exit) + 2
        if len(c) < need:
            return st
        st.ready = True
        st.bar_start = float(s["start"][-1])
        st.bar_close_ts = st.bar_start + cfg.tf_seconds
        st.close = float(c[-1])
        st.entry_level = float(np.max(h[-cfg.n_entry - 1:-1]))
        st.exit_level = float(np.min(l[-cfg.n_exit - 1:-1]))
        st.ema = float(ind.ema(c, cfg.ema_period)[-1])
        st.atr = float(ind.atr(h, l, c, cfg.atr_period)[-1])
        st.breakout = st.close > st.entry_level and st.close > st.ema
        st.channel_exit = st.close < st.exit_level
        return st

    def update(self, mk: MarketState) -> tuple[TrendState, bool]:
        """Recalcula sólo al cerrar una vela 6 h. Devuelve (estado, ¿vela nueva?)."""
        prev = self.state.get(mk.product_id)
        s = mk.htf.closed()
        if not len(s["start"]) or (prev is not None and prev.bar_start == s["start"][-1]):
            return prev or TrendState(), False
        st = self.compute(s, self.cfg)
        self.state[mk.product_id] = st
        return st, prev is not None and st.ready


# ─────────────────────────────────────────────────────────────────────────────
# Oráculo BTC (Market Leader Veto)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class OracleState:
    veto: bool = False
    reason: str = "RISK-ON"
    change_5m_pct: float = 0.0
    cvd_ratio: float = 0.0
    obi: float = 0.0
    since: float = 0.0
    last_trigger: float = 0.0


class BTCOracle:
    def __init__(self, cfg: OracleConfig, obi_levels: int, obi_band_bps: float = 10.0) -> None:
        self.cfg = cfg
        self.obi_levels = obi_levels
        self.obi_band_bps = obi_band_bps
        self.state = OracleState()

    def update(self, btc: MarketState) -> tuple[OracleState, bool]:
        """Devuelve (estado, cambió_de_estado)."""
        c = self.cfg
        chg = btc.price_change_pct(c.window_s)
        delta, vol = btc.cvd_window(c.window_s)
        cvd_ratio = delta / vol if vol > 0 else 0.0
        obi = btc.book.obi_band(self.obi_band_bps, self.obi_levels) if btc.book.initialized else 0.0
        reasons: list[str] = []
        if chg <= -c.max_drop_pct:
            reasons.append(f"caída 5m {chg:.2f}%")
        if cvd_ratio <= c.cvd_veto_ratio:
            reasons.append(f"CVD 5m {cvd_ratio:+.2f}")
        if obi <= c.obi_veto:
            reasons.append(f"OBI {obi:+.2f}")
        now = time.time()
        prev = self.state.veto
        s = self.state
        s.change_5m_pct, s.cvd_ratio, s.obi = chg, cvd_ratio, obi
        if reasons:
            s.last_trigger = now
            if not s.veto:
                s.since = now
            s.veto = True
            s.reason = "VETO BAJISTA: " + ", ".join(reasons)
        elif s.veto and now - s.last_trigger >= c.release_cooldown_s:
            s.veto = False
            s.reason = "RISK-ON"
            s.since = now
        return s, s.veto != prev


# ─────────────────────────────────────────────────────────────────────────────
# Motor de estrategias
# ─────────────────────────────────────────────────────────────────────────────
class StrategyEngine:
    def __init__(
        self, scfg: StrategyConfig, fcfg: FeeConfig, ocfg: OracleConfig, pairs: dict[str, PairConfig],
        tcfg: TrendConfig | None = None, strategies: tuple[str, ...] = ("BREAKOUT_ORDERFLOW", "MEAN_REVERSION_MICRO"),
    ) -> None:
        self.tcfg = tcfg or TrendConfig()
        self.trend = TrendStrategy(self.tcfg)
        self.strategies = set(strategies)
        self.cfg = scfg
        self.fees = fcfg
        self.pairs = pairs
        self.regime = RegimeDetector(scfg)
        self.oracle = BTCOracle(ocfg, scfg.obi_levels, scfg.obi_band_bps)
        self.maker_fee = fcfg.maker_fee
        self.taker_fee = fcfg.taker_fee
        self.analytics: dict[str, PairAnalytics] = {p: PairAnalytics(p) for p in pairs}
        self._tf_cache: dict[str, tuple[Any, dict[str, np.ndarray], tuple[Regime, dict[str, float]]]] = {}

    def set_fee_rates(self, maker: float, taker: float) -> None:
        # Modelo conservador: nunca por debajo de lo configurado
        self.maker_fee = max(maker, self.fees.maker_fee)
        self.taker_fee = max(taker, self.fees.taker_fee)

    # ── análisis base ───────────────────────────────────────────────────────
    def analyze(self, mk: MarketState) -> tuple[PairAnalytics, dict[str, np.ndarray], dict[str, np.ndarray]]:
        c = self.cfg
        a = self.analytics[mk.product_id]
        a.ts = time.time()
        a.best_bid = mk.book.best_bid or float("nan")
        a.best_ask = mk.book.best_ask or float("nan")
        a.price = mk.book.mid or mk.last_price
        a.spread_pct = mk.book.spread_pct if mk.book.spread_pct is not None else float("nan")
        a.obi = mk.book.obi_band(c.obi_band_bps, c.obi_levels) if mk.book.initialized else 0.0
        a.vwap = mk.vwap.vwap
        a.vwap_sigma = mk.vwap.sigma
        delta, vol = mk.cvd_window(c.cvd_window_s)
        a.cvd_5m = delta
        a.cvd_ratio_5m = delta / vol if vol > 0 else 0.0

        s1 = mk.series(1, last_n=400)
        # La TF de señal sólo cambia al cerrar una barra: se cachea (evita re-muestrear
        # decenas de miles de velas 1m en cada evaluación).
        key = (int(time.time() // (c.regime_tf_minutes * 60)), len(mk.bars) > 0)
        cached = self._tf_cache.get(mk.product_id)
        if cached is None or cached[0] != key:
            s5 = mk.series(c.regime_tf_minutes)
            cached = (key, s5, self.regime.classify(s5))
            self._tf_cache[mk.product_id] = cached
        _, s5, (regime, m) = cached
        a.regime = regime
        a.hurst = m.get("hurst", float("nan"))
        a.adx = m.get("adx", float("nan"))
        a.atr = m.get("atr", float("nan"))
        a.atr_pct = m.get("atr_pct", float("nan"))
        a.vol_percentile = m.get("vol_percentile", float("nan"))
        a.rsi_1m = float(ind.rsi(s1["close"], c.rsi_period)[-1]) if len(s1["close"]) > c.rsi_period + 1 else float("nan")
        a.rsi_5m = float(ind.rsi(s5["close"], c.rsi_period)[-1]) if len(s5["close"]) > c.rsi_period + 1 else float("nan")

        # Recorrido proyectado "de referencia" para el panel aunque no haya señal
        if not math.isnan(a.atr) and a.price > 0:
            mult = c.tp_atr_mult_mr if regime is Regime.RANGE else c.tp_atr_mult_trend
            tp = a.price + mult * a.atr
            fh = fee_hurdle(a.price, tp, a.spread_pct if not math.isnan(a.spread_pct) else 0.0,
                            self.maker_fee, self.fees)
            a.projected_move_pct, a.fee_ok, a.fee_reason = fh.projected_pct, fh.ok, fh.reason
        return a, s1, s5

    def update_oracle(self, btc: MarketState) -> tuple[OracleState, bool]:
        return self.oracle.update(btc)

    # ── evaluación completa ────────────────────────────────────────────────
    def evaluate(self, mk: MarketState) -> tuple[PairAnalytics, Signal | None]:
        pcfg = self.pairs[mk.product_id]
        a, s1, s5 = self.analyze(mk)
        if not mk.is_ready or a.regime is Regime.WARMUP:
            a.signal_state = "WARMUP"
            return a, None
        if math.isnan(a.spread_pct) or a.spread_pct > pcfg.max_spread_pct:
            a.signal_state = f"SPREAD {a.spread_pct:.3f}% > {pcfg.max_spread_pct:.3f}%"
            return a, None
        if a.regime is Regime.NOISE:
            a.signal_state = "BLOQUEADO: régimen NOISE"
            return a, None

        signal: Signal | None = None
        if a.regime is Regime.TREND and pcfg.allow_trend:
            signal = self._breakout(mk, a, s5)
        elif a.regime is Regime.RANGE and pcfg.allow_mean_reversion:
            signal = self._mean_reversion(mk, a, s1, s5)
        else:
            a.signal_state = f"{a.regime.value}: estrategia no habilitada para {pcfg.role.value}"
            return a, None
        if signal is None:
            return a, None

        # Filtro de microestructura: nunca comprar contra un muro de venta
        if a.obi < self.cfg.obi_min_long:
            a.signal_state = f"VETO OBI {a.obi:+.2f} < {self.cfg.obi_min_long:+.2f}"
            return a, None

        # Oráculo BTC
        if mk.product_id != ORACLE_PRODUCT and self.oracle.state.veto:
            a.signal_state = f"VETO ORÁCULO BTC ({self.oracle.state.reason})"
            return a, None

        # Ajuste de TP a zonas de liquidez (frontrun del muro de venta)
        signal.take_profit = self._snap_tp_to_liquidity(mk, signal.ref_price, signal.take_profit)

        fh = fee_hurdle(signal.ref_price, signal.take_profit, a.spread_pct, self.maker_fee, self.fees)
        signal.projected_move_pct = fh.projected_pct
        a.projected_move_pct, a.fee_ok, a.fee_reason = fh.projected_pct, fh.ok, fh.reason
        if not fh.ok:
            a.signal_state = f"FEE-HURDLE: {fh.reason}"
            return a, None

        a.signal_state = f"SEÑAL {signal.strategy.value}"
        return a, signal

    # ── Estrategia de tendencia 6 h ─────────────────────────────────────────
    def evaluate_trend(self, mk: MarketState) -> tuple[PairAnalytics, Signal | None, TrendState]:
        a, _, _ = self.analyze(mk)
        pcfg = self.pairs[mk.product_id]
        st, new_bar = self.trend.update(mk)
        a.trend_entry_level, a.trend_ema, a.trend_exit_level, a.trend_atr = (
            st.entry_level, st.ema, st.exit_level, st.atr)
        a.fee_ok, a.fee_reason = True, "N/A (tendencia sin TP fijo)"
        if not st.ready:
            a.signal_state = "TREND_6H: calentando (faltan velas 6 h)"
            return a, None, st
        dist = (st.entry_level - a.price) / a.price * 100 if a.price else float("nan")
        a.projected_move_pct = dist
        base = (f"TREND_6H: cierre {st.close:.6g} · ruptura {st.entry_level:.6g} · EMA200 {st.ema:.6g}")
        if not st.breakout:
            why = "bajo EMA200" if st.close <= st.ema else f"falta {dist:+.2f}% para la ruptura"
            a.signal_state = f"{base} · sin señal ({why})"
            return a, None, st
        if time.time() - st.bar_close_ts > self.tcfg.entry_window_s:
            a.signal_state = f"{base} · ruptura en la vela anterior (ventana de entrada expirada)"
            return a, None, st
        if not mk.is_ready or math.isnan(a.spread_pct) or a.spread_pct > pcfg.max_spread_pct:
            a.signal_state = f"{base} · ruptura bloqueada por spread"
            return a, None, st
        if a.obi < self.cfg.obi_min_long:
            a.signal_state = f"{base} · ruptura bloqueada por OBI {a.obi:+.2f}"
            return a, None, st
        if mk.product_id != ORACLE_PRODUCT and self.oracle.state.veto:
            a.signal_state = f"{base} · ruptura bloqueada por el oráculo BTC"
            return a, None, st
        price = a.best_bid if not math.isnan(a.best_bid) else a.price
        sig = Signal(
            product_id=mk.product_id, strategy=StrategyName.TREND, side=Side.BUY, regime=Regime.TREND,
            ref_price=price, stop_price=price - self.tcfg.k_atr * st.atr,
            take_profit=price * self.tcfg.far_tp_multiple, atr=st.atr, obi=a.obi,
            projected_move_pct=float("nan"),
            reason=f"Ruptura Donchian {self.tcfg.n_entry}×6h ({st.close:.6g} > {st.entry_level:.6g}) sobre EMA200",
        )
        sig.ts = st.bar_close_ts
        a.signal_state = f"SEÑAL TREND_6H · {base}"
        return a, sig, st

    # ── Estrategia 1: Breakout + Order Flow ─────────────────────────────────
    def _breakout(self, mk: MarketState, a: PairAnalytics, s5: dict[str, np.ndarray]) -> Signal | None:
        c = self.cfg
        close, high, low, vol = s5["close"], s5["high"], s5["low"], s5["volume"]
        sq = ind.squeeze_state(high, low, close, c.bb_period, c.bb_std, c.kc_mult, c.atr_period)
        _, bb_u, _ = ind.bollinger(close, c.bb_period, c.bb_std)
        recent = sq[-(c.squeeze_lookback + 1):-1]
        released = bool(np.any(recent)) and not bool(sq[-1])
        price = a.best_bid if not math.isnan(a.best_bid) else a.price
        avg_vol = float(np.mean(vol[-21:-1])) if len(vol) > 21 else float("nan")
        # volumen de la barra en curso (misma duración que la TF de señal)
        live_vol = sum(t.size for t in mk.window_trades(c.regime_tf_minutes * 60))
        checks = {
            "squeeze_release": released or not c.breakout_require_squeeze,
            "close>BB_up": close[-1] > bb_u[-1],
            "price>VWAP": not math.isnan(a.vwap) and price > a.vwap,
            "volumen": not math.isnan(avg_vol) and max(vol[-1], live_vol) >= c.volume_confirm_mult * avg_vol,
            "CVD+": a.cvd_5m > 0 and a.cvd_ratio_5m > 0.05,
        }
        failed = [k for k, v in checks.items() if not v]
        if failed:
            a.signal_state = "TREND sin setup: " + ",".join(failed)
            return None
        stop = price - c.sl_atr_mult * a.atr
        tp = price + c.tp_atr_mult_trend * a.atr
        return Signal(
            product_id=mk.product_id, strategy=StrategyName.BREAKOUT, side=Side.BUY,
            regime=a.regime, ref_price=price, stop_price=stop, take_profit=tp, atr=a.atr,
            obi=a.obi, projected_move_pct=0.0,
            reason=f"Squeeze release + BB↑ + VWAP + Vol + CVD {a.cvd_ratio_5m:+.2f}",
        )

    # ── Estrategia 2: Mean-Reversion + Microestructura ──────────────────────
    def _mean_reversion(
        self, mk: MarketState, a: PairAnalytics, s1: dict[str, np.ndarray], s5: dict[str, np.ndarray]
    ) -> Signal | None:
        c = self.cfg
        price = a.best_bid if not math.isnan(a.best_bid) else a.price
        lower_band = a.vwap - c.vwap_sigma_entry * a.vwap_sigma
        trades = mk.window_trades(c.absorption_window_s)
        buy_v = sum(t.size for t in trades if t.signed > 0)
        sell_v = sum(t.size for t in trades if t.signed < 0)
        rng = (max(t.price for t in trades) - min(t.price for t in trades)) if trades else float("inf")
        absorption = (
            sell_v >= c.absorption_sell_buy_ratio * max(buy_v, 1e-12)
            and rng <= c.absorption_max_range_atr * a.atr
            and a.obi > 0
        )
        checks = {
            "VWAP-2.5σ": not math.isnan(lower_band) and price <= lower_band,
            "RSI1m": not math.isnan(a.rsi_1m) and a.rsi_1m <= c.rsi_1m_oversold,
            "RSI5m": not math.isnan(a.rsi_5m) and a.rsi_5m <= c.rsi_5m_oversold,
            "absorción": absorption or not c.mr_require_absorption,
        }
        failed = [k for k, v in checks.items() if not v]
        if failed:
            a.signal_state = "RANGE sin setup: " + ",".join(failed)
            return None
        stop = price - c.sl_atr_mult * a.atr
        tp = min(a.vwap, price + c.tp_atr_mult_mr * a.atr)
        return Signal(
            product_id=mk.product_id, strategy=StrategyName.MEAN_REVERSION, side=Side.BUY,
            regime=a.regime, ref_price=price, stop_price=stop, take_profit=tp, atr=a.atr,
            obi=a.obi, projected_move_pct=0.0,
            reason=f"VWAP-{c.vwap_sigma_entry}σ + RSI {a.rsi_1m:.0f}/{a.rsi_5m:.0f} + absorción {sell_v/max(buy_v,1e-12):.1f}x",
        )

    def _snap_tp_to_liquidity(self, mk: MarketState, entry: float, tp: float) -> float:
        """Coloca el TP justo por debajo del primer muro de venta relevante."""
        walls = mk.book.ask_walls(200, self.cfg.wall_multiple)
        min_tp = entry * (1 + self.fees.min_projected_move_pct / 100.0)
        candidates = [p for p, _ in walls if min_tp <= p < tp]
        if not candidates:
            return tp
        tick = mk.quote_increment
        return min(candidates) - tick

    def liquidity_tp_levels(self, mk: MarketState, entry: float, r_value: float) -> list[float]:
        """Precios de TP parcial: R-múltiplos ajustados a muros de liquidez."""
        levels: list[float] = []
        walls = sorted(p for p, _ in mk.book.ask_walls(200, self.cfg.wall_multiple))
        for r in self.cfg.partial_tp_r:
            target = entry + r * r_value
            near = [w for w in walls if target * 0.997 <= w <= target * 1.003]
            levels.append((min(near) - mk.quote_increment) if near else target)
        return levels
