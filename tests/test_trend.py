"""Estrategia de tendencia 6 h: paridad con la investigación y ciclo de vida en PAPER."""
import asyncio
import dataclasses

import numpy as np
import pytest

import indicators as ind
from config import SETTINGS, TrendConfig
from database import AsyncDatabase
from exchange import PaperGateway, ProductMeta
from execution_handler import ExecutionHandler
from market_data import MarketState
from models import Regime, Side, Signal, StrategyName
from risk_manager import RiskManager
from strategy_engine import StrategyEngine, TrendStrategy
from telegram_bot import TelegramNotifier

PID = "SOL-USDC"


def _series(n=400, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
    h = c * (1 + rng.uniform(0, 0.01, n))
    l = c * (1 - rng.uniform(0, 0.01, n))
    return {"start": np.arange(n) * 21600.0, "open": c, "high": h, "low": l, "close": c}


def test_live_rules_match_research_rules():
    cfg = TrendConfig()
    s = _series()
    ema = ind.ema(s["close"], cfg.ema_period)
    hits = 0
    for i in range(cfg.ema_period + 5, len(s["close"])):
        sub = {k: v[: i + 1] for k, v in s.items()}
        st = TrendStrategy.compute(sub, cfg)
        research_entry = s["close"][i] > np.max(s["high"][i - cfg.n_entry:i]) and s["close"][i] > ema[i]
        research_exit = s["close"][i] < np.min(s["low"][i - cfg.n_exit:i])
        assert st.breakout == research_entry and st.channel_exit == research_exit
        hits += research_entry
    assert hits > 0


def _book(mk, bid, ask):
    ups = []
    for i in range(20):
        ups.append({"side": "bid", "price_level": f"{bid - i * 0.01:.2f}", "new_quantity": "50"})
        ups.append({"side": "offer", "price_level": f"{ask + i * 0.01:.2f}", "new_quantity": "50"})
    mk.book.apply_snapshot(ups)


@pytest.mark.asyncio
async def test_trend_position_lifecycle(tmp_path):
    settings = dataclasses.replace(
        SETTINGS, execution=dataclasses.replace(SETTINGS.execution, exchange_stop_min_update_s=0.0))
    markets = {p: MarketState(p) for p in settings.pairs}
    mk = markets[PID]
    mk.quote_increment = 0.01
    _book(mk, 99.99, 100.01)
    gw = PaperGateway(markets, 10_000.0, 0.004, 0.006)
    for p in settings.pairs:
        gw.products[p] = ProductMeta(p, 0.001, 0.01, 0.001, 1.0)
    strat = StrategyEngine(settings.strategy, settings.fees, settings.oracle, settings.pairs,
                           settings.trend, ("TREND_6H",))
    risk = RiskManager(settings.risk, settings.pairs, settings.execution.min_net_rr)
    db = AsyncDatabase(tmp_path / "t.db")
    ex = ExecutionHandler(settings, gw, markets, strat, risk, db, TelegramNotifier("", ""),
                          validator=lambda s: (True, "OK"))
    atr6 = 2.0
    sig = Signal(PID, StrategyName.TREND, Side.BUY, Regime.TREND, 99.99, 99.99 - 5 * atr6, 99.99 * 4,
                 atr6, 0.2, float("nan"), "test")
    sizing = risk.size_position(sig, 99.99, 10_000, 10_000, 0.004, 0.006, gw.meta(PID), [])
    assert sizing.ok and sizing.risk_usd <= 100.0 + 1e-6
    ex.submit_entry(sig, sizing)
    await asyncio.sleep(0.05)
    await gw.on_market_trade(PID, 99.98, 10_000.0)
    await asyncio.wait_for(ex.entry_tasks[PID], 2.0)
    pos = next(iter(ex.positions.values()))
    assert len(pos.tp_levels) == 1 and pos.tp_levels[0].price > pos.entry_price * 3.9
    brackets = [o for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"]
    assert len(brackets) == 1 and brackets[0].size == pytest.approx(pos.size)

    _book(mk, 115.0, 115.02)                               # +7.5 ATR: trailing sin breakeven
    await ex.manage_positions()
    await asyncio.gather(*ex._stop_update_tasks.values())
    assert not pos.breakeven_armed
    assert pos.stop_price == pytest.approx(115.0 - 5 * atr6)
    trig = [o.stop_trigger for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"]
    assert trig and trig[0] == pytest.approx(round(pos.stop_price * 0.998, 2), abs=0.011)

    _book(mk, pos.stop_price - 0.05, pos.stop_price - 0.03)
    await ex.manage_positions()
    for _ in range(50):
        if not ex.positions:
            break
        await asyncio.sleep(0.02)
    t = (await db.run(db.sync.trades))[0]
    assert t["exit_reason"] == "TRAILING STOP" and t["net_pnl"] > 0
    assert gw.balances["USDC"] == pytest.approx(10_000 + t["net_pnl"], abs=1e-6)
    await db.close()
