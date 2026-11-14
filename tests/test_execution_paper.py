"""Integración: flujo completo en PAPER sin red."""
import asyncio
import time

import pytest

from config import SETTINGS
from database import AsyncDatabase
from exchange import PaperGateway, ProductMeta
from execution_handler import ExecutionHandler
from market_data import MarketState
from models import Regime, Side, Signal, StrategyName
from risk_manager import RiskManager
from strategy_engine import StrategyEngine
from telegram_bot import TelegramNotifier

PID = "SOL-USDC"


def _book(mk: MarketState, bid: float, ask: float, depth: float = 50.0) -> None:
    ups = []
    for i in range(20):
        ups.append({"side": "bid", "price_level": f"{bid - i*0.01:.2f}", "new_quantity": str(depth)})
        ups.append({"side": "offer", "price_level": f"{ask + i*0.01:.2f}", "new_quantity": str(depth)})
    mk.book.apply_snapshot(ups)


@pytest.mark.asyncio
async def test_entry_tp_breakeven_and_stop(tmp_path):
    markets = {p: MarketState(p) for p in SETTINGS.pairs}
    mk = markets[PID]
    mk.quote_increment = 0.01
    _book(mk, 99.99, 100.01)
    gw = PaperGateway(markets, 10_000.0, 0.004, 0.006)
    for p in SETTINGS.pairs:
        gw.products[p] = ProductMeta(p, 0.001, 0.01, 0.001, 1.0)
    strat = StrategyEngine(SETTINGS.strategy, SETTINGS.fees, SETTINGS.oracle, SETTINGS.pairs)
    strat.analytics[PID].atr = 2.0
    risk = RiskManager(SETTINGS.risk, SETTINGS.pairs, SETTINGS.execution.min_net_rr)
    db = AsyncDatabase(tmp_path / "s.db")
    ex = ExecutionHandler(SETTINGS, gw, markets, strat, risk, db, TelegramNotifier("", ""),
                          validator=lambda s: (True, "OK"))

    sig = Signal(PID, StrategyName.BREAKOUT, Side.BUY, Regime.TREND, 99.99, 96.0, 112.0, 2.0, 0.3, 12.0, "test")
    sizing = risk.size_position(sig, 99.99, 10_000, 10_000, 0.004, 0.006, gw.meta(PID), [])
    assert sizing.ok
    ex.submit_entry(sig, sizing)
    await asyncio.sleep(0.05)
    # el precio atraviesa nuestro bid → llenado maker completo
    await gw.on_market_trade(PID, 99.98, 1000.0)
    await asyncio.wait_for(ex.entry_tasks[PID], 2.0)
    assert len(ex.positions) == 1
    pos = next(iter(ex.positions.values()))
    assert pos.entry_price == pytest.approx(100.00)   # bid + 1 tick, sin cruzar el ask
    assert pos.entry_fee_paid == pytest.approx(pos.size * 100.00 * 0.004)
    tp_orders = [t for t in ex.orders_by_cid.values() if t.purpose == "TAKE_PROFIT" and not t.status.is_terminal]
    assert len(tp_orders) >= 2

    # sube a +1.2R → Fee-Adjusted Breakeven armado
    _book(mk, 105.0, 105.02)
    await ex.manage_positions()
    assert pos.breakeven_armed and pos.stop_price > pos.entry_price

    # TP1 se llena por flujo
    tp1 = min(tp_orders, key=lambda t: t.price)
    await gw.on_market_trade(PID, tp1.price + 0.01, 1000.0)
    await asyncio.sleep(0)
    assert pos.size < pos.initial_size

    # caída bajo el stop → cierre total a mercado
    _book(mk, pos.stop_price - 0.05, pos.stop_price - 0.03)
    await ex.manage_positions()
    for _ in range(50):
        if not ex.positions:
            break
        await asyncio.sleep(0.02)
    assert not ex.positions
    trades = await db.run(db.sync.trades)
    assert len(trades) == 1
    t = trades[0]
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"])
    assert t["net_pnl"] > 0          # breakeven ajustado + TP parcial ⇒ trade neto positivo
    # balance paper coherente: equity final = inicial + neto
    assert gw.balances.get("SOL", 0) == pytest.approx(0.0, abs=1e-9)
    assert gw.balances["USDC"] == pytest.approx(10_000 + t["net_pnl"], abs=1e-6)
    await db.close()


@pytest.mark.asyncio
async def test_post_only_never_crosses_and_chase_aborts_on_escape(tmp_path):
    markets = {p: MarketState(p) for p in SETTINGS.pairs}
    mk = markets[PID]
    _book(mk, 99.99, 100.01)
    gw = PaperGateway(markets, 10_000.0, 0.004, 0.006)
    for p in SETTINGS.pairs:
        gw.products[p] = ProductMeta(p, 0.001, 0.01, 0.001, 1.0)
    from exchange import PostOnlyRejected
    with pytest.raises(PostOnlyRejected):
        await gw.place_post_only(PID, Side.BUY, 1.0, 100.01, "x")

    strat = StrategyEngine(SETTINGS.strategy, SETTINGS.fees, SETTINGS.oracle, SETTINGS.pairs)
    risk = RiskManager(SETTINGS.risk, SETTINGS.pairs, SETTINGS.execution.min_net_rr)
    db = AsyncDatabase(tmp_path / "s.db")
    ex = ExecutionHandler(SETTINGS, gw, markets, strat, risk, db, TelegramNotifier("", ""),
                          validator=lambda s: (True, "OK"))
    sig = Signal(PID, StrategyName.BREAKOUT, Side.BUY, Regime.TREND, 99.99, 96.0, 112.0, 2.0, 0.3, 12.0, "test")
    sizing = risk.size_position(sig, 99.99, 10_000, 10_000, 0.004, 0.006, gw.meta(PID), [])
    # el precio se escapa por encima del deslizamiento máximo antes de enviar
    _book(mk, 101.0, 101.02)
    res = await ex._chase(sig, sizing)
    assert res.filled == 0 and "escapó" in res.reason
    await db.close()


async def _open_position(tmp_path, settings):
    markets = {p: MarketState(p) for p in settings.pairs}
    mk = markets[PID]
    mk.quote_increment = 0.01
    _book(mk, 99.99, 100.01)
    gw = PaperGateway(markets, 10_000.0, 0.004, 0.006)
    for p in settings.pairs:
        gw.products[p] = ProductMeta(p, 0.001, 0.01, 0.001, 1.0)
    strat = StrategyEngine(settings.strategy, settings.fees, settings.oracle, settings.pairs)
    strat.analytics[PID].atr = 2.0
    risk = RiskManager(settings.risk, settings.pairs, settings.execution.min_net_rr)
    db = AsyncDatabase(tmp_path / "s.db")
    ex = ExecutionHandler(settings, gw, markets, strat, risk, db, TelegramNotifier("", ""),
                          validator=lambda s: (True, "OK"))
    sig = Signal(PID, StrategyName.BREAKOUT, Side.BUY, Regime.TREND, 99.99, 96.0, 112.0, 2.0, 0.3, 12.0, "test")
    sizing = risk.size_position(sig, 99.99, 10_000, 10_000, 0.004, 0.006, gw.meta(PID), [])
    ex.submit_entry(sig, sizing)
    await asyncio.sleep(0.05)
    await gw.on_market_trade(PID, 99.98, 1000.0)
    await asyncio.wait_for(ex.entry_tasks[PID], 2.0)
    return gw, ex, db, mk


@pytest.mark.asyncio
async def test_exchange_stop_protects_when_engine_is_down(tmp_path):
    gw, ex, db, mk = await _open_position(tmp_path, SETTINGS)
    pos = next(iter(ex.positions.values()))
    brackets = [o for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"]
    assert brackets and sum(o.size for o in brackets) == pytest.approx(pos.size)
    trigger = brackets[0].stop_trigger
    assert trigger < pos.stop_price                       # respaldo por debajo del stop software
    # el motor NO gestiona (caído): sólo llegan trades del mercado en caída
    await gw.on_market_trade(PID, trigger - 0.5, 50.0)
    for _ in range(50):
        if not ex.positions:
            break
        await asyncio.sleep(0.02)
    assert not ex.positions
    t = (await db.run(db.sync.trades))[0]
    assert t["exit_reason"] == "STOP EN EXCHANGE"
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"])
    assert gw.balances["USDC"] == pytest.approx(10_000 + t["net_pnl"], abs=1e-6)
    await db.close()


@pytest.mark.asyncio
async def test_brackets_follow_breakeven(tmp_path):
    import dataclasses
    fast = dataclasses.replace(SETTINGS, execution=dataclasses.replace(SETTINGS.execution, exchange_stop_min_update_s=0.0))
    gw, ex, db, mk = await _open_position(tmp_path, fast)
    pos = next(iter(ex.positions.values()))
    before = {o.stop_trigger for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"}
    _book(mk, 105.0, 105.02)                              # +1.2R → breakeven
    await ex.manage_positions()
    await asyncio.gather(*ex._stop_update_tasks.values())
    after = {o.stop_trigger for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN"}
    assert pos.breakeven_armed
    assert min(after) > max(before) and min(after) >= pos.entry_price * 0.99
    open_sz = sum(o.leaves for o in gw.orders.values() if o.stop_trigger > 0 and o.status.value == "OPEN")
    assert open_sz == pytest.approx(pos.size)             # todo el inventario sigue protegido
    await db.close()
