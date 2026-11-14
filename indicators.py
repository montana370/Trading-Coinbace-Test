"""
indicators.py — Indicadores cuantitativos vectorizados (numpy puro, sin I/O).

Todas las funciones aceptan np.ndarray float64 y devuelven arrays alineados
(con NaN en el período de calentamiento) o escalares.
"""
from __future__ import annotations

import numpy as np

NDArray = np.ndarray


def ema(x: NDArray, period: int) -> NDArray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = np.mean(x[:period])
    for i in range(period, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def wilder_smooth(x: NDArray, period: int) -> NDArray:
    """Media de Wilder (RMA)."""
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < period:
        return out
    out[period - 1] = np.mean(x[:period])
    for i in range(period, len(x)):
        out[i] = (out[i - 1] * (period - 1) + x[i]) / period
    return out


def true_range(high: NDArray, low: NDArray, close: NDArray) -> NDArray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])


def atr(high: NDArray, low: NDArray, close: NDArray, period: int = 14) -> NDArray:
    return wilder_smooth(true_range(high, low, close), period)


def adx(high: NDArray, low: NDArray, close: NDArray, period: int = 14) -> NDArray:
    n = len(close)
    out = np.full(n, np.nan)
    if n < 2 * period + 1:
        return out
    up = np.diff(high, prepend=high[0])
    down = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(high, low, close)
    tr_s = wilder_smooth(tr, period)
    plus_di = 100 * wilder_smooth(plus_dm, period) / np.where(tr_s == 0, np.nan, tr_s)
    minus_di = 100 * wilder_smooth(minus_dm, period) / np.where(tr_s == 0, np.nan, tr_s)
    denom = plus_di + minus_di
    dx = 100 * np.abs(plus_di - minus_di) / np.where(denom == 0, np.nan, denom)
    dx = np.nan_to_num(dx, nan=0.0)
    first = period - 1
    adx_vals = wilder_smooth(dx[first:], period)
    out[first:] = adx_vals
    return out


def rsi(close: NDArray, period: int = 14) -> NDArray:
    out = np.full(len(close), np.nan)
    if len(close) <= period:
        return out
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_g = wilder_smooth(gains, period)
    avg_l = wilder_smooth(losses, period)
    rs = avg_g / np.where(avg_l == 0, np.nan, avg_l)
    vals = 100 - 100 / (1 + rs)
    vals = np.where((avg_l == 0) & ~np.isnan(avg_g), 100.0, vals)
    out[1:] = vals
    return out


def bollinger(close: NDArray, period: int = 20, k: float = 2.0) -> tuple[NDArray, NDArray, NDArray]:
    n = len(close)
    mid = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if n < period:
        return mid, upper, lower
    windows = np.lib.stride_tricks.sliding_window_view(close, period)
    m = windows.mean(axis=1)
    s = windows.std(axis=1, ddof=0)
    mid[period - 1:] = m
    upper[period - 1:] = m + k * s
    lower[period - 1:] = m - k * s
    return mid, upper, lower


def keltner(
    high: NDArray, low: NDArray, close: NDArray, period: int = 20, mult: float = 1.5, atr_period: int = 14
) -> tuple[NDArray, NDArray, NDArray]:
    mid = ema(close, period)
    a = atr(high, low, close, atr_period)
    return mid, mid + mult * a, mid - mult * a


def squeeze_state(
    high: NDArray, low: NDArray, close: NDArray, bb_period: int, bb_std: float, kc_mult: float, atr_period: int
) -> NDArray:
    """True cuando las Bandas de Bollinger están dentro del Canal de Keltner."""
    _, bb_u, bb_l = bollinger(close, bb_period, bb_std)
    _, kc_u, kc_l = keltner(high, low, close, bb_period, kc_mult, atr_period)
    with np.errstate(invalid="ignore"):
        return (bb_u < kc_u) & (bb_l > kc_l)


def hurst_exponent(prices: NDArray, max_lag: int = 20) -> float:
    """
    Exponente de Hurst por escalado de varianza de incrementos de log-precio:
        std(x[t+lag] - x[t]) ∝ lag^H
    H > 0.5 → persistente (tendencia); H < 0.5 → anti-persistente (reversión).
    """
    if len(prices) < max_lag * 3:
        return float("nan")
    x = np.log(np.asarray(prices, dtype=float))
    lags = np.arange(2, max_lag + 1)
    tau = np.array([np.std(x[lag:] - x[:-lag]) for lag in lags])
    valid = tau > 0
    if valid.sum() < 5:
        return float("nan")
    slope, _ = np.polyfit(np.log(lags[valid]), np.log(tau[valid]), 1)
    return float(np.clip(slope, 0.0, 1.0))


def percentile_rank(series: NDArray, value: float) -> float:
    s = series[~np.isnan(series)]
    if len(s) == 0 or np.isnan(value):
        return float("nan")
    return float(np.mean(s <= value))


def order_book_imbalance(bid_sizes: NDArray, ask_sizes: NDArray) -> float:
    b = float(np.sum(bid_sizes))
    a = float(np.sum(ask_sizes))
    total = b + a
    return 0.0 if total <= 0 else (b - a) / total
