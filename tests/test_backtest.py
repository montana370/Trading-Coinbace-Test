import numpy as np

import indicators as ind
from backtest import rolling_hurst, rolling_pctile


def test_rolling_hurst_matches_live_implementation():
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 600)))
    roll = rolling_hurst(close, 120)
    for i in (119, 250, 599):
        assert abs(roll[i] - ind.hurst_exponent(close[i - 119:i + 1], max_lag=20)) < 1e-9
    assert np.isnan(roll[118])


def test_rolling_pctile_matches_percentile_rank():
    rng = np.random.default_rng(1)
    x = rng.normal(size=400)
    out = rolling_pctile(x, 50)
    for i in (49, 200, 399):
        assert abs(out[i] - ind.percentile_rank(x[i - 49:i + 1], x[i])) < 1e-12
