import numpy as np

import indicators as ind


def test_hurst_trending_vs_mean_reverting():
    rng = np.random.default_rng(7)
    # paseo aleatorio con deriva persistente (incrementos AR(1) positivos)
    inc = np.zeros(600)
    for i in range(1, 600):
        inc[i] = 0.8 * inc[i - 1] + rng.normal(0, 0.001)
    trending = 100 * np.exp(np.cumsum(inc))
    # proceso Ornstein-Uhlenbeck fuertemente revertido
    x = np.zeros(600)
    for i in range(1, 600):
        x[i] = 0.3 * x[i - 1] + rng.normal(0, 0.001)
    mean_rev = 100 * np.exp(x)
    assert ind.hurst_exponent(trending) > 0.55
    assert ind.hurst_exponent(mean_rev) < 0.45


def test_atr_rsi_adx_shapes_and_ranges():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 300))
    high = close + rng.uniform(0.1, 0.5, 300)
    low = close - rng.uniform(0.1, 0.5, 300)
    a = ind.atr(high, low, close, 14)
    r = ind.rsi(close, 14)
    d = ind.adx(high, low, close, 14)
    assert len(a) == len(r) == len(d) == 300
    assert np.nanmin(a) > 0
    assert 0 <= np.nanmin(r) and np.nanmax(r) <= 100
    assert 0 <= np.nanmin(d) and np.nanmax(d) <= 100


def test_obi():
    assert ind.order_book_imbalance(np.array([3.0]), np.array([1.0])) == 0.5
    assert ind.order_book_imbalance(np.array([]), np.array([])) == 0.0
