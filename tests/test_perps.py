"""Perpetuos: cortos, contratos, margen y paridad con research_perps."""
import asyncio
import dataclasses

import numpy as np
import pytest

import indicators as ind
from config import SETTINGS, TrendConfig
from database import AsyncDatabase
from exchange import ExchangeError, PaperGateway, ProductMeta
from execution_handler import ExecutionHandler
from market_data import MarketState
from models import Regime, Side, Signal, StrategyName
from risk_manager import RiskManager, fee_adjusted_breakeven, net_reward_risk
from strategy_engine import StrategyEngine, TrendStrategy
from telegram_bot import TelegramNotifier

PID = "BTC-USDC"
CS = 0.01   # 1 contrato = 0.01 BTC


def test_short_rules_match_research():
    cfg = TrendConfig(n_entry=20, n_exit=10, k_atr=2.5)
    rng = np.random.default_rng(9)
    c = 100 * np.exp(np.cumsum(rng.normal(-0.001, 0.02, 400)))
    s = {"start": np.arange(400) * 21600.0, "open": c, "close": c,
         "high": c * (1 + rng.uniform(0, 0.01, 400)), "low": c * (1 - rng.uniform(0, 0.01, 400))}
    ema = ind.ema(c, 200)
    hits = 0
    for i in range(210, 400):
        st = TrendStrategy.compute({k: v[: i + 1] for k, v in s.items()}, cfg)
        assert st.breakdown == (c[i] < np.min(s["low"][i - 20:i]) and c[i] < ema[i])
        assert st.channel_exit_short == (c[i] > np.max(s["high"][i - 10:i]))
        hits += st.breakdown
    assert hits > 0


def test_short_math():
    _, risk_u, rr = net_reward_risk(100.0, 105.0, 80.0, 0.0002, 0.0005)
    assert risk_u == pytest.approx(5.0 + 100 * 0.0002 + 105 * 0.0005)
    assert rr > 3
    be = fee_adjusted_breakeven(100.0, 0.0002, 0.0005, short=True)
    qty = 2.0
    assert (100.0 * qty * (1 - 0.0002)) - be * qty * (1 + 0.0005) == pytest.approx(0.0, abs=1e-9)


def _book(mk, bid, ask, depth=5.0):
    ups = []
    for i in range(20):
        ups.append({"side": "bid", "price_level": f"{bid - i * 5:.2f}", "new_quantity": str(depth)})
        ups.append({"side": "offer", "price_level": f"{ask + i * 5:.2f}", "new_quantity": str(depth)})
    mk.book.apply_snapshot(ups)


async def _setup(tmp_path, equity=10_000.0):
    settings = dataclasses.replace(
        SETTINGS, execution=dataclasses.replace(SETTINGS.execution, exchange_stop_min_update_s=0.0),
        risk=dataclasses.replace(SETTINGS.risk, max_leverage=2.0),
        trend=dataclasses.replace(SETTINGS.trend, k_atr=2.5, allow_short=True))
    markets = {p: MarketState(p) for p in settings.pairs}
    mk = markets[PID]
    mk.quote_increment = 5.0
    _book(mk, 60_000.0, 60_020.0)
    gw = PaperGateway(markets, equity, 0.0002, 0.0005, perps=True, max_leverage=2.0)
    for p in settings.pairs:
        gw.products[p] = ProductMeta(p, CS, 5.0, CS, 0.0, contract_size=CS, exchange_id="BIP-20DEC30-CDE",
                                     is_future=True)
    strat = StrategyEngine(settings.strategy, settings.fees, settings.oracle, settings.pairs,
                           settings.trend, ("TREND_6H",))
    risk = RiskManager(settings.risk, settings.pairs, settings.execution.min_net_rr)
    db = AsyncDatabase(tmp_path / "p.db")
    ex = ExecutionHandler(settings, gw, markets, strat, risk, db, TelegramNotifier("", ""),
                          validator=lambda s: (True, "OK"))
    return settings, gw, ex, db, mk, risk


async def _open_short(gw, ex, mk, risk, atr=600.0):
    sig = Signal(PID, StrategyName.TREND, Side.SELL, Regime.TREND, 60_020.0, 60_020.0 + 2.5 * atr,
                 60_020.0 / 4, atr, -0.2, float("nan"), "test short")
    sizing = risk.size_position(sig, 60_020.0, 10_000, 20_000, 0.0002, 0.0005, gw.meta(PID), [])
    assert sizing.ok, sizing.reason
    assert sizing.base_size == pytest.approx(round(sizing.base_size / CS) * CS)   # contratos enteros
    ex.submit_entry(sig, sizing)
    await asyncio.sleep(0.05)
    await gw.on_market_trade(PID, 60_030.0, 100.0)        # el precio atraviesa nuestra venta límite
    await asyncio.wait_for(ex.entry_tasks[PID], 2.0)
    return next(iter(ex.positions.values()))


@pytest.mark.asyncio
async def test_short_lifecycle_trailing_and_close(tmp_path):
    settings, gw, ex, db, mk, risk = await _setup(tmp_path)
    pos = await _open_short(gw, ex, mk, risk)
    assert pos.is_short and pos.entry_price == pytest.approx(60_015.0)       # ask − 1 tick
    assert gw.position(PID)[0] == pytest.approx(-pos.size)
    br = [o for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"]
    assert len(br) == 1 and br[0].side is Side.BUY and br[0].stop_trigger > pos.stop_price
    # cae el precio: el trailing baja el stop
    _book(mk, 55_000.0, 55_020.0)
    await ex.manage_positions()
    await asyncio.gather(*ex._stop_update_tasks.values())
    assert pos.stop_price == pytest.approx(55_020.0 + 2.5 * 600.0)
    new_trig = [o.stop_trigger for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"]
    assert new_trig and new_trig[0] < 60_000.0
    # rebota hasta el stop: cierre a mercado (compra)
    _book(mk, pos.stop_price + 5, pos.stop_price + 25)
    await ex.manage_positions()
    for _ in range(50):
        if not ex.positions:
            break
        await asyncio.sleep(0.02)
    t = (await db.run(db.sync.trades))[0]
    assert t["side"] == "SELL" and t["exit_reason"] == "TRAILING STOP"
    assert t["net_pnl"] > 0 and t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"])
    assert gw.position(PID)[0] == pytest.approx(0.0)
    assert gw.balances["USDC"] == pytest.approx(10_000 + t["net_pnl"], abs=1e-6)
    await db.close()


@pytest.mark.asyncio
async def test_short_exchange_stop_when_engine_down(tmp_path):
    settings, gw, ex, db, mk, risk = await _setup(tmp_path)
    pos = await _open_short(gw, ex, mk, risk)
    trig = [o for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"][0].stop_trigger
    await gw.on_market_trade(PID, trig + 10, 1.0)          # sube el precio y el motor no actúa
    for _ in range(50):
        if not ex.positions:
            break
        await asyncio.sleep(0.02)
    t = (await db.run(db.sync.trades))[0]
    assert t["exit_reason"] == "STOP EN EXCHANGE" and t["net_pnl"] < 0
    assert gw.balances["USDC"] == pytest.approx(10_000 + t["net_pnl"], abs=1e-6)
    await db.close()


@pytest.mark.asyncio
async def test_perps_margin_limit_and_contract_rounding(tmp_path):
    settings, gw, ex, db, mk, risk = await _setup(tmp_path, equity=1_000.0)
    assert gw._size_str(PID, 0.0349) == "3"                 # 0.0349 BTC → 3 contratos
    with pytest.raises(ExchangeError, match="MARGIN"):       # 0.05 BTC ≈ 3.000 $ > 2× 1.000 $
        await gw.place_post_only(PID, Side.SELL, 0.05, 60_020.0, "x")
    await db.close()
