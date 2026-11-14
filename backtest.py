"""
backtest.py — Backtester de cartera sobre velas de 1 minuto de Coinbase.

Reutiliza el código del bot (indicadores, detector de régimen, Fee-Hurdle,
RR neto, sizing, breakeven ajustado por comisiones) y simula la cartera:
máx. posiciones simultáneas, Oráculo BTC, comisiones maker/taker, TPs
parciales, breakeven a +1.2R, trailing ATR y circuit breakers.

LIMITACIONES (honestas): las velas no contienen libro L2 ni flujo de órdenes.
    * OBI L2 → no simulable (se asume que pasa; el filtro real es más estricto).
    * CVD → proxy: Σ signo(close−open)·volumen / Σ volumen de las velas 1m.
    * Absorción → proxy: mecha inferior ≥ 50 % del rango de la última vela 1m.
    * Spread → constante = mitad del máximo permitido del par.
    * Entrada maker: se llena si la vela 1m siguiente cotiza por debajo de la
      apertura (precio ≤ ref·(1+deslizamiento máx.)); si no, la señal se pierde.

Uso:
    python backtest.py                         # parámetros actuales de config.py
    python backtest.py --grid                  # calibración walk-forward
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

import indicators as ind
from config import ORACLE_PRODUCT, SETTINGS, PairConfig, StrategyConfig
from exchange import ProductMeta
from history import load
from models import Regime, Side, Signal, StrategyName
from risk_manager import RiskManager, fee_adjusted_breakeven
from strategy_engine import fee_hurdle

OUT = Path("data/backtest")


# ═════════════════════════════════════════════════════════════════════════════
# Features (se calculan una vez por par; la calibración sólo cambia umbrales)
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Features:
    pid: str
    t1: np.ndarray            # 1m: inicio de vela
    o1: np.ndarray
    h1: np.ndarray
    l1: np.ndarray
    c1: np.ndarray
    t5_close: np.ndarray      # 5m: instante de cierre (inicio + 300)
    c5: np.ndarray
    idx1_at5: np.ndarray      # índice de la vela 1m siguiente al cierre 5m
    atr: np.ndarray
    atr_pct: np.ndarray
    adx: np.ndarray
    hurst: np.ndarray
    vol_pctile: np.ndarray
    released: np.ndarray      # squeeze liberado en las últimas N barras
    above_bb: np.ndarray
    vol_ok: np.ndarray
    rsi5: np.ndarray
    rsi1: np.ndarray
    vwap: np.ndarray
    vwap_sigma: np.ndarray
    cvd_ratio: np.ndarray
    absorption: np.ndarray
    chg_5m: np.ndarray        # variación % de la última barra 5m (oráculo)


def rolling_hurst(close: np.ndarray, window: int, max_lag: int = 20) -> np.ndarray:
    """Versión vectorizada exacta de indicators.hurst_exponent sobre ventanas deslizantes."""
    x = np.log(close)
    n = len(x)
    lags = np.arange(2, max_lag + 1)
    log_tau = np.full((len(lags), n), np.nan)
    for k, lag in enumerate(lags):
        d = np.full(n, np.nan)
        d[lag:] = x[lag:] - x[:-lag]
        w = window - lag                        # nº de diferencias dentro de la ventana
        dd = np.nan_to_num(d)
        c1 = np.concatenate(([0.0], np.cumsum(dd)))
        c2 = np.concatenate(([0.0], np.cumsum(dd * dd)))
        end = np.arange(n) + 1
        start = end - w
        valid = start >= lag
        s1 = np.where(valid, c1[end] - c1[np.clip(start, 0, None)], np.nan)
        s2 = np.where(valid, c2[end] - c2[np.clip(start, 0, None)], np.nan)
        var = s2 / w - (s1 / w) ** 2
        tau = np.sqrt(np.clip(var, 1e-30, None))
        log_tau[k] = np.where(valid, np.log(tau), np.nan)
    lx = np.log(lags)[:, None]
    lxm = lx - lx.mean()
    slope = (lxm * (log_tau - np.nanmean(log_tau, axis=0))).sum(axis=0) / (lxm ** 2).sum()
    slope[: window - 1] = np.nan
    return np.clip(slope, 0.0, 1.0)


def rolling_pctile(series: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(series), np.nan)
    if len(series) < window:
        return out
    win = np.lib.stride_tricks.sliding_window_view(series, window)
    last = win[:, -1][:, None]
    valid = ~np.isnan(win)
    out[window - 1:] = ((win <= last) & valid).sum(axis=1) / np.maximum(valid.sum(axis=1), 1)
    return out


def build_features(pid: str, raw: dict[str, np.ndarray], c: StrategyConfig) -> Features:
    t1, o1, h1, l1, c1, v1 = (raw[k] for k in ("start", "open", "high", "low", "close", "volume"))
    # ── barras de la temporalidad de señal (regime_tf_minutes) ────────────
    span = c.regime_tf_minutes * 60
    bucket = (t1 // span).astype(np.int64)
    starts = np.flatnonzero(np.diff(bucket, prepend=bucket[0] - 1))
    ends = np.append(starts[1:], len(t1))
    o5 = o1[starts]
    h5 = np.maximum.reduceat(h1, starts)
    l5 = np.minimum.reduceat(l1, starts)
    c5 = c1[ends - 1]
    v5 = np.add.reduceat(v1, starts)
    t5_close = bucket[starts].astype(float) * span + span
    atr = ind.atr(h5, l5, c5, c.atr_period)
    atr_pct = atr / c5 * 100
    adx = ind.adx(h5, l5, c5, c.adx_period)
    hurst = rolling_hurst(c5, c.hurst_window)
    vol_pctile = rolling_pctile(atr_pct, c.vol_percentile_window)
    sq = ind.squeeze_state(h5, l5, c5, c.bb_period, c.bb_std, c.kc_mult, c.atr_period)
    sq_prev = np.lib.stride_tricks.sliding_window_view(np.concatenate((np.zeros(c.squeeze_lookback, bool), sq[:-1])),
                                                       c.squeeze_lookback).any(axis=1)
    released = sq_prev & ~sq
    _, bb_u, _ = ind.bollinger(c5, c.bb_period, c.bb_std)
    above_bb = c5 > bb_u
    vol_avg = np.full(len(v5), np.nan)
    if len(v5) > 21:
        cs = np.concatenate(([0.0], np.cumsum(v5)))
        idx = np.arange(21, len(v5) + 1)
        vol_avg[20:] = (cs[idx - 1] - cs[idx - 21]) / 20
    vol_ok = v5 >= c.volume_confirm_mult * vol_avg
    rsi5 = ind.rsi(c5, c.rsi_period)
    rsi1_full = ind.rsi(c1, c.rsi_period)
    rsi1 = rsi1_full[ends - 1]
    # ── VWAP de sesión UTC sobre 1m ───────────────────────────────────────
    day = (t1 // 86400).astype(np.int64)
    tp = (h1 + l1 + c1) / 3
    new_day = np.flatnonzero(np.diff(day, prepend=day[0] - 1))
    pv, vv, p2v = tp * v1, v1, tp * tp * v1

    def session_cumsum(a: np.ndarray) -> np.ndarray:
        cs = np.cumsum(a)
        offs = np.zeros_like(cs)
        for s in new_day[1:]:
            offs[s:] = cs[s - 1]
        return cs - offs

    cpv, cv, cp2v = session_cumsum(pv), session_cumsum(vv), session_cumsum(p2v)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap1 = cpv / cv
        sig1 = np.sqrt(np.clip(cp2v / cv - vwap1 ** 2, 0, None))
    vwap = vwap1[ends - 1]
    vwap_sigma = sig1[ends - 1]
    # ── proxies de flujo ──────────────────────────────────────────────────
    signed = np.sign(c1 - o1) * v1
    cs_s = np.concatenate(([0.0], np.cumsum(signed)))
    cs_v = np.concatenate(([0.0], np.cumsum(v1)))
    e = ends
    s_ = np.maximum(e - 5, 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cvd_ratio = (cs_s[e] - cs_s[s_]) / (cs_v[e] - cs_v[s_])
    last = ends - 1
    rng = h1[last] - l1[last]
    lower_wick = np.minimum(o1[last], c1[last]) - l1[last]
    with np.errstate(invalid="ignore", divide="ignore"):
        absorption = (rng > 0) & (lower_wick / rng >= 0.5)
    # variación de 5 minutos (oráculo BTC) medida en velas 1m, igual que en vivo
    chg = np.zeros(len(c5))
    prev = np.maximum(ends - 6, 0)
    chg = (c1[ends - 1] - c1[prev]) / c1[prev] * 100
    return Features(
        pid=pid, t1=t1, o1=o1, h1=h1, l1=l1, c1=c1, t5_close=t5_close, c5=c5,
        idx1_at5=ends, atr=atr, atr_pct=atr_pct, adx=adx, hurst=hurst, vol_pctile=vol_pctile,
        released=released, above_bb=above_bb, vol_ok=vol_ok, rsi5=rsi5, rsi1=rsi1, vwap=vwap,
        vwap_sigma=vwap_sigma, cvd_ratio=np.nan_to_num(cvd_ratio), absorption=absorption, chg_5m=chg,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Simulación de cartera
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class BTPosition:
    pid: str
    strategy: str
    entry: float
    size: float
    initial_size: float
    stop: float
    initial_stop: float
    tp: float
    atr: float
    entry_fee: float
    opened: float
    i1: int                       # índice 1m siguiente a gestionar
    tps: list[list[float]] = field(default_factory=list)   # [precio, fracción, hecho]
    be_armed: bool = False
    highest: float = 0.0
    gross: float = 0.0
    exit_fees: float = 0.0


@dataclass
class Result:
    params: dict[str, Any]
    trades: list[dict[str, Any]]
    equity_end: float
    start_equity: float
    days: float
    metrics: dict[str, Any] = field(default_factory=dict)


def classify(f: Features, i: int, c: StrategyConfig) -> Regime:
    h, a, vp = f.hurst[i], f.adx[i], f.vol_pctile[i]
    if math.isnan(h) or math.isnan(a) or math.isnan(vp):
        return Regime.WARMUP
    if vp >= c.vol_pct_noise_high or vp <= c.vol_pct_noise_low:
        return Regime.NOISE
    if h >= c.hurst_trend and a >= c.adx_trend:
        return Regime.TREND
    if h <= c.hurst_mr and a <= c.adx_range:
        return Regime.RANGE
    return Regime.NOISE


def signal_at(f: Features, i: int, c: StrategyConfig, pcfg: PairConfig) -> Signal | None:
    regime = classify(f, i, c)
    price, atr = f.c5[i], f.atr[i]
    if regime is Regime.TREND and pcfg.allow_trend:
        if ((f.released[i] or not c.breakout_require_squeeze) and f.above_bb[i] and price > f.vwap[i]
                and f.vol_ok[i] and f.cvd_ratio[i] > 0.05):
            return Signal(f.pid, StrategyName.BREAKOUT, Side.BUY, regime, price, price - c.sl_atr_mult * atr,
                          price + c.tp_atr_mult_trend * atr, atr, 0.0, 0.0, "bt", ts=f.t5_close[i])
    elif regime is Regime.RANGE and pcfg.allow_mean_reversion:
        band = f.vwap[i] - c.vwap_sigma_entry * f.vwap_sigma[i]
        if (price <= band and f.rsi1[i] <= c.rsi_1m_oversold and f.rsi5[i] <= c.rsi_5m_oversold
                and (f.absorption[i] or not c.mr_require_absorption)):
            return Signal(f.pid, StrategyName.MEAN_REVERSION, Side.BUY, regime, price,
                          price - c.sl_atr_mult * atr, min(f.vwap[i], price + c.tp_atr_mult_mr * atr),
                          atr, 0.0, 0.0, "bt", ts=f.t5_close[i])
    return None


def regime_share(feats: dict[str, Features], c: StrategyConfig, t_from: float, t_to: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pid, f in feats.items():
        mask = (f.t5_close >= t_from) & (f.t5_close < t_to)
        counts: dict[str, int] = {}
        for i in np.flatnonzero(mask):
            r = classify(f, i, c).value
            counts[r] = counts.get(r, 0) + 1
        tot = max(sum(counts.values()), 1)
        out[pid] = {k: round(v / tot * 100, 1) for k, v in counts.items()}
    return out


def simulate(feats: dict[str, Features], c: StrategyConfig, t_from: float, t_to: float,
             start_equity: float = 10_000.0) -> Result:
    s = SETTINGS
    maker, taker = s.fees.maker_fee, s.fees.taker_fee
    rm = RiskManager(s.risk, s.pairs, s.execution.min_net_rr)
    rm.cfg = replace(s.risk, use_kelly=False)
    equity = cash = start_equity
    trades: list[dict[str, Any]] = []
    open_pos: dict[str, BTPosition] = {}
    # línea temporal de evaluaciones (cierres 5m) de todos los pares
    events: list[tuple[float, str, int]] = []
    for pid, f in feats.items():
        for i in np.flatnonzero((f.t5_close >= t_from) & (f.t5_close < t_to)):
            events.append((f.t5_close[i], pid, int(i)))
    events.sort()
    btc = feats.get(ORACLE_PRODUCT)
    btc_idx = {t: i for i, t in enumerate(btc.t5_close)} if btc is not None else {}
    oracle_last_trigger = -1e18
    day, day_start_eq, dd_lock_until, pause_until, streak = -1, equity, 0.0, 0.0, 0
    slip = s.execution.max_chase_slippage_pct / 100

    def manage(pos: BTPosition, until_t: float) -> bool:
        """Gestiona la posición con velas 1m hasta `until_t`. True si se cerró."""
        nonlocal cash
        f = feats[pos.pid]
        n = len(f.t1)
        while pos.i1 < n and f.t1[pos.i1] + 60 <= until_t:
            j = pos.i1
            o, h, l = f.o1[j], f.h1[j], f.l1[j]
            # 1) stop (peor caso primero), salida taker
            if l <= pos.stop:
                px = min(pos.stop, o)
                fee = px * pos.size * taker
                pos.gross += (px - pos.entry) * pos.size
                pos.exit_fees += fee
                cash += px * pos.size - fee
                pos.size = 0.0
                close_trade(pos, f.t1[j] + 60, "TRAILING STOP" if pos.be_armed and pos.stop > pos.entry
                            else "BREAKEVEN STOP" if pos.be_armed else "STOP LOSS")
                return True
            # 2) TPs maker en reposo
            for lv in pos.tps:
                if not lv[2] and h > lv[0]:
                    pending = [x for x in pos.tps if not x[2]]
                    qty = pos.size if lv is pending[-1] else min(pos.size, pos.initial_size * lv[1])
                    fee = lv[0] * qty * maker
                    pos.gross += (lv[0] - pos.entry) * qty
                    pos.exit_fees += fee
                    cash += lv[0] * qty - fee
                    pos.size -= qty
                    lv[2] = 1.0
            if pos.size <= 1e-12:
                close_trade(pos, f.t1[j] + 60, "TAKE PROFIT")
                return True
            # 3) breakeven y trailing (aplican desde la vela siguiente)
            pos.highest = max(pos.highest, h)
            r_val = pos.entry - pos.initial_stop
            if not pos.be_armed and (h - pos.entry) / r_val >= c.breakeven_trigger_r:
                pos.be_armed = True
                pos.stop = max(pos.stop, fee_adjusted_breakeven(pos.entry, maker, taker))
            if pos.be_armed:
                pos.stop = max(pos.stop, pos.highest - c.trail_atr_mult * pos.atr)
            pos.i1 += 1
        return False

    def close_trade(pos: BTPosition, t: float, reason: str) -> None:
        nonlocal streak, pause_until
        fees = pos.entry_fee + pos.exit_fees
        net = pos.gross - fees
        trades.append({
            "product_id": pos.pid, "strategy": pos.strategy, "entry_price": pos.entry,
            "size": pos.initial_size, "gross_pnl": pos.gross, "fees": fees, "net_pnl": net,
            "net_pct": net / (pos.entry * pos.initial_size) * 100, "opened_at": pos.opened,
            "closed_at": t, "exit_reason": reason, "initial_stop": pos.initial_stop, "take_profit": pos.tp,
        })
        open_pos.pop(pos.pid, None)
        streak = streak + 1 if net < 0 else 0
        if streak >= s.risk.max_consecutive_losses:
            pause_until = t + s.risk.loss_streak_pause_hours * 3600
            streak = 0

    for t, pid, i in events:
        # gestionar posiciones abiertas hasta este instante
        for p in list(open_pos.values()):
            manage(p, t)
        mark = cash + sum(p.size * feats[p.pid].c1[min(p.i1, len(feats[p.pid].c1) - 1)] for p in open_pos.values())
        equity = mark
        d = int(t // 86400)
        if d != day:
            day, day_start_eq = d, equity
        if (day_start_eq - equity) / day_start_eq >= s.risk.max_daily_drawdown and t >= dd_lock_until:
            for p in list(open_pos.values()):     # kill-switch: liquidar a mercado
                f = feats[p.pid]
                px = f.c1[min(p.i1, len(f.c1) - 1)]
                fee = px * p.size * taker
                p.gross += (px - p.entry) * p.size
                p.exit_fees += fee
                cash += px * p.size - fee
                p.size = 0
                close_trade(p, t, "DAILY DD KILL")
            dd_lock_until = t + s.risk.daily_dd_lock_hours * 3600
        # oráculo BTC (proxy: caída 5m o CVD proxy)
        if btc is not None and t in btc_idx:
            k = btc_idx[t]
            if btc.chg_5m[k] <= -s.oracle.max_drop_pct or btc.cvd_ratio[k] <= s.oracle.cvd_veto_ratio:
                oracle_last_trigger = t
        if t < dd_lock_until or t < pause_until or pid in open_pos:
            continue
        if len(open_pos) >= s.risk.max_open_positions:
            continue
        f = feats[pid]
        sig = signal_at(f, i, c, s.pairs[pid])
        if sig is None:
            continue
        if pid != ORACLE_PRODUCT and t - oracle_last_trigger < s.oracle.release_cooldown_s:
            continue
        spread = s.pairs[pid].max_spread_pct * 0.5
        if not fee_hurdle(sig.ref_price, sig.take_profit, spread, maker, s.fees).ok:
            continue
        # entrada maker: vela 1m siguiente
        j = int(f.idx1_at5[i])
        if j >= len(f.t1):
            continue
        entry = f.o1[j]
        if entry > sig.ref_price * (1 + slip) or not f.l1[j] < entry:
            continue
        sig.ref_price = entry
        meta = ProductMeta(pid, 1e-8, 1e-6, 1e-8, 1.0)
        sz = rm.size_position(sig, entry, equity, cash, maker, taker, meta, [])
        if not sz.ok:
            continue
        fee = entry * sz.base_size * maker
        cash -= entry * sz.base_size + fee
        r_val = entry - sig.stop_price
        tps = []
        for r, fr in zip(c.partial_tp_r, c.partial_tp_fractions):
            px = entry + r * r_val
            if px < sig.take_profit:
                tps.append([px, fr, 0.0])
        tps.append([sig.take_profit, 1.0 - sum(x[1] for x in tps), 0.0])
        open_pos[pid] = BTPosition(pid, sig.strategy.value, entry, sz.base_size, sz.base_size, sig.stop_price,
                                   sig.stop_price, sig.take_profit, sig.atr, fee, f.t1[j], j, tps, highest=entry)

    for p in list(open_pos.values()):   # cierre al final del periodo
        f = feats[p.pid]
        manage(p, t_to)
        if p.pid in open_pos:
            px = f.c1[min(p.i1, len(f.c1) - 1)]
            fee = px * p.size * taker
            p.gross += (px - p.entry) * p.size
            p.exit_fees += fee
            cash += px * p.size - fee
            p.size = 0
            close_trade(p, t_to, "FIN DEL PERIODO")
    res = Result(params=asdict(c), trades=trades, equity_end=cash, start_equity=start_equity,
                 days=(t_to - t_from) / 86400)
    res.metrics = metrics(res)
    return res


def metrics(r: Result) -> dict[str, Any]:
    tr = r.trades
    nets = np.array([t["net_pnl"] for t in tr]) if tr else np.array([])
    wins, losses = nets[nets > 0], -nets[nets < 0]
    eq = r.start_equity + np.concatenate(([0.0], np.cumsum(nets)))
    peak = np.maximum.accumulate(eq)
    max_dd = float(np.max((peak - eq) / peak) * 100) if len(eq) else 0.0
    by_pair: dict[str, dict[str, float]] = {}
    for t in tr:
        b = by_pair.setdefault(t["product_id"], {"trades": 0, "net": 0.0})
        b["trades"] += 1
        b["net"] += t["net_pnl"]
    by_strat: dict[str, dict[str, float]] = {}
    for t in tr:
        b = by_strat.setdefault(t["strategy"], {"trades": 0, "net": 0.0})
        b["trades"] += 1
        b["net"] += t["net_pnl"]
    return {
        "trades": len(tr),
        "trades_per_week": round(len(tr) / max(r.days, 1e-9) * 7, 2),
        "net_pnl": round(float(nets.sum()), 2),
        "return_pct": round(float(nets.sum()) / r.start_equity * 100, 2),
        "gross_pnl": round(sum(t["gross_pnl"] for t in tr), 2),
        "fees": round(sum(t["fees"] for t in tr), 2),
        "win_rate": round(len(wins) / len(tr) * 100, 1) if tr else None,
        "profit_factor": round(float(wins.sum() / losses.sum()), 2) if len(losses) and losses.sum() > 0 else None,
        "avg_net_pct": round(float(np.mean([t["net_pct"] for t in tr])), 3) if tr else None,
        "max_dd_pct": round(max_dd, 2),
        "by_pair": {k: {"trades": v["trades"], "net": round(v["net"], 2)} for k, v in by_pair.items()},
        "by_strategy": {k: {"trades": v["trades"], "net": round(v["net"], 2)} for k, v in by_strat.items()},
        "exits": {k: sum(1 for t in tr if t["exit_reason"] == k) for k in sorted({t["exit_reason"] for t in tr})},
    }


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════
REGIME_GRID = {
    "hurst_trend": [0.50, 0.53, 0.56],
    "adx_trend": [18.0, 22.0, 26.0],
    "hurst_mr": [0.44, 0.47, 0.50],
    "adx_range": [20.0, 25.0, 30.0],
    "vol_pct_noise_high": [0.97, 0.995],
}
SETUP_GRID = {
    "breakout_require_squeeze": [True, False],
    "squeeze_lookback": [6, 12],
    "volume_confirm_mult": [1.2, 1.5],
    "vwap_sigma_entry": [2.0, 2.5],
    "rsi_1m_oversold": [25.0, 30.0],
    "rsi_5m_oversold": [35.0, 40.0],
    "mr_require_absorption": [True, False],
}
EXIT_GRID = {
    "sl_atr_mult": [1.5, 2.0, 3.0],
    "tp_atr_mult_trend": [6.0, 9.0, 12.0],
    "tp_atr_mult_mr": [4.0, 6.0, 8.0],
    "trail_atr_mult": [2.5, 4.0],
}
TIMEFRAMES = [60, 120, 240]


def load_features(c: StrategyConfig, pairs: list[str]) -> dict[str, Features]:
    return {pid: build_features(pid, load(pid), c) for pid in pairs}


def score(m: dict[str, Any]) -> float:
    """Prioriza robustez: retorno y PF, penaliza drawdown; exige un mínimo de operaciones."""
    if m["trades"] < 12:
        return -1e9
    pf = m["profit_factor"] if m["profit_factor"] is not None else (3.0 if m["net_pnl"] > 0 else 0.0)
    return m["return_pct"] - 0.5 * m["max_dd_pct"] + 2.0 * min(pf, 3.0)


def grid_search(feats: dict[str, Features], base: StrategyConfig, grid: dict[str, list[Any]],
                t_from: float, t_to: float) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    keys = list(grid)
    rows = []
    for values in itertools.product(*grid.values()):
        p = dict(zip(keys, values))
        m = simulate(feats, replace(base, **p), t_from, t_to).metrics
        rows.append((score(m), p, m))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--pairs", nargs="*", default=list(SETTINGS.pairs))
    ap.add_argument("--train-frac", type=float, default=2 / 3)
    ap.add_argument("--tfs", nargs="*", type=int, default=TIMEFRAMES)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base = SETTINGS.strategy
    t0 = time.time()
    report: dict[str, Any] = {"generated_at": time.time(), "pairs": args.pairs,
                              "fees": {"maker": SETTINGS.fees.maker_fee, "taker": SETTINGS.fees.taker_fee}}

    def window(feats: dict[str, Features], c: StrategyConfig) -> tuple[float, float, float]:
        warm = c.vol_percentile_window + c.hurst_window
        t_min = max(f.t5_close[min(warm, len(f.t5_close) - 1)] for f in feats.values())
        t_max = min(f.t5_close[-1] for f in feats.values())
        return t_min, t_min + (t_max - t_min) * args.train_frac, t_max

    feats = load_features(base, args.pairs)
    t_min, t_split, t_max = window(feats, base)
    report["current"] = {
        "timeframe_min": base.regime_tf_minutes,
        "train": simulate(feats, base, t_min, t_split).metrics,
        "test": simulate(feats, base, t_split, t_max).metrics,
        "regimes": regime_share(feats, base, t_min, t_max),
        "period_days": round((t_max - t_min) / 86400, 1),
    }
    print("ACTUAL:", json.dumps(report["current"], ensure_ascii=False), flush=True)

    if args.grid:
        candidates = []
        for tf in args.tfs:
            c_tf = replace(base, regime_tf_minutes=tf, breakout_require_squeeze=False, mr_require_absorption=False,
                           vwap_sigma_entry=2.0, rsi_1m_oversold=30.0, rsi_5m_oversold=40.0, volume_confirm_mult=1.2,
                           tp_atr_mult_trend=9.0, tp_atr_mult_mr=6.0,
                           # ventanas en barras: a TF alta se acortan para conservar historia útil
                           hurst_window=100 if tf >= 120 else 120,
                           vol_percentile_window=180 if tf >= 120 else 288)
            f_tf = load_features(c_tf, args.pairs)
            a, b, z = window(f_tf, c_tf)
            t1 = time.time()
            stage1 = grid_search(f_tf, c_tf, REGIME_GRID, a, b)
            print(f"[{tf}m] etapa 1: {len(stage1)} combinaciones en {time.time() - t1:.0f}s · "
                  f"rentables en train: {sum(1 for r in stage1 if r[2]['net_pnl'] > 0 and r[2]['trades'] >= 12)}",
                  flush=True)
            # etapa 1 con setups relajados para que el régimen tenga operaciones que evaluar
            for _, p1, _ in stage1[:3]:
                stage2 = grid_search(f_tf, replace(c_tf, **p1), SETUP_GRID, a, b)
                for sc2, p2, _ in stage2[:2]:
                    if sc2 <= -1e9:
                        continue
                    for sc3, p3, m_train in grid_search(f_tf, replace(c_tf, **p1, **p2), EXIT_GRID, a, b)[:2]:
                        if sc3 <= -1e9:
                            continue
                        params = {"regime_tf_minutes": tf, "hurst_window": c_tf.hurst_window,
                                  "vol_percentile_window": c_tf.vol_percentile_window, **p1, **p2, **p3}
                        m_test = simulate(f_tf, replace(base, **params), b, z).metrics
                        candidates.append({"params": params, "score_train": round(sc3, 2), "train": m_train,
                                           "test": m_test, "test_days": round((z - b) / 86400, 1)})
                        print(json.dumps({k: candidates[-1][k] for k in ("params", "score_train")} |
                                         {"train": {k: m_train[k] for k in ("trades", "return_pct", "profit_factor", "max_dd_pct")},
                                          "test": {k: m_test[k] for k in ("trades", "return_pct", "profit_factor", "max_dd_pct")}},
                                         ensure_ascii=False), flush=True)
        # elección final por TRAIN (el test es fuera de muestra y no se usa para elegir)
        candidates.sort(key=lambda r: r["score_train"], reverse=True)
        report["candidates"] = candidates
        report["selected"] = candidates[0] if candidates else None
    path = OUT / "report.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=float))
    print(f"Informe: {path} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
