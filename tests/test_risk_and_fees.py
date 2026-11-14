import time

import pytest

from config import SETTINGS, FeeConfig
from exchange import ProductMeta
from models import BotStatus, Regime, Side, Signal, StrategyName
from risk_manager import RiskManager, fee_adjusted_breakeven, net_reward_risk
from strategy_engine import fee_hurdle


def test_fee_hurdle_rejects_short_moves_and_accepts_long_ones():
    cfg = FeeConfig(maker_fee=0.004, min_projected_move_pct=1.25, min_cost_multiple=3.0)
    # coste RT = 0.8% + 0.02% spread → requerido 2.46%
    bad = fee_hurdle(100.0, 102.0, 0.02, 0.004, cfg)
    good = fee_hurdle(100.0, 102.6, 0.02, 0.004, cfg)
    assert not bad.ok and good.ok
    assert bad.required_pct == pytest.approx(2.46)


def test_fee_adjusted_breakeven_is_exactly_zero_net():
    entry, fe, fx = 100.0, 0.004, 0.006
    be = fee_adjusted_breakeven(entry, fe, fx)
    qty = 3.0
    net = be * qty * (1 - fx) - entry * qty * (1 + fe)
    assert net == pytest.approx(0.0, abs=1e-9)


def _signal(stop=96.0, tp=110.0):
    return Signal("SOL-USDC", StrategyName.BREAKOUT, Side.BUY, Regime.TREND, 100.0, stop, tp, 2.0, 0.3, 10.0, "t")


def test_sizing_respects_risk_and_allocation_caps():
    rm = RiskManager(SETTINGS.risk, SETTINGS.pairs, min_net_rr=1.8)
    meta = ProductMeta("SOL-USDC", 0.001, 0.01, 0.001, 1.0)
    res = rm.size_position(_signal(), 100.0, 10_000.0, 10_000.0, 0.004, 0.006, meta, [])
    assert res.ok
    _, risk_u, _ = net_reward_risk(100.0, 96.0, 110.0, 0.004, 0.006)
    assert res.risk_usd <= 100.0 + 1e-6               # 1 % de 10k
    assert res.notional <= 0.35 * 10_000 + 1e-6       # asignación SOL 35 %
    assert res.base_size == pytest.approx(min(100.0 / risk_u, 35.0), rel=1e-3)


def test_sizing_vetoes_poor_net_rr():
    rm = RiskManager(SETTINGS.risk, SETTINGS.pairs, min_net_rr=1.8)
    meta = ProductMeta("SOL-USDC", 0.001, 0.01, 0.001, 1.0)
    res = rm.size_position(_signal(stop=96.0, tp=104.0), 100.0, 10_000.0, 10_000.0, 0.004, 0.006, meta, [])
    assert not res.ok and "RR" in res.reason


def test_circuit_breakers():
    rm = RiskManager(SETTINGS.risk, SETTINGS.pairs, min_net_rr=1.8)
    assert rm.on_equity(10_000) is None
    evt = rm.on_equity(9_690)                         # −3.1 %
    assert evt and evt.kind == "DAILY_DD" and rm.status is BotStatus.KILLED
    ok, _ = rm.resume()
    assert not ok                                     # bloqueo 24h no se salta con /resume

    rm2 = RiskManager(SETTINGS.risk, SETTINGS.pairs, min_net_rr=1.8)
    assert rm2.on_trade_closed(-1) is None
    assert rm2.on_trade_closed(-1) is None
    evt = rm2.on_trade_closed(-1)
    assert evt and evt.kind == "LOSS_STREAK" and rm2.status is BotStatus.PAUSED
    assert rm2.state.loss_pause_until > time.time() + 5.9 * 3600


def test_watchdog_and_position_limit():
    rm = RiskManager(SETTINGS.risk, SETTINGS.pairs, min_net_rr=1.8)
    evt = rm.watchdog(heartbeat_age_s=9.0, api_latency_ms=50, ws_connected=True)
    assert evt and evt.kind == "SAFE_ON" and not rm.entries_allowed
    evt = rm.watchdog(heartbeat_age_s=1.0, api_latency_ms=50, ws_connected=True)
    assert evt and evt.kind == "SAFE_OFF" and rm.entries_allowed
    ok, why = rm.can_open("ETH-USDC", {"SOL-USDC"}, {"SUI-USDC"})
    assert not ok and "Máx" in why
