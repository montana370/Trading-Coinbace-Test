"""
Integración del Core Engine completo sin red: se sustituyen las llamadas REST
y se inyectan mensajes WebSocket con el formato real de Coinbase Advanced Trade
(l2_data, market_trades, heartbeats). Verifica: dispatch, snapshots para el
Dashboard, oráculo BTC, bus de comandos (PAUSE/RESUME/PANIC) y apagado limpio.
"""
import asyncio
import dataclasses
import math
import time

import numpy as np
import pytest

import main as engine_mod
from config import SETTINGS
from exchange import ProductMeta

PRICES = {"SOL-USDC": 150.0, "BTC-USDC": 90_000.0, "ETH-USDC": 3_000.0, "SUI-USDC": 3.0}


def _candles(pid: str, n: int) -> list[dict]:
    rng = np.random.default_rng(abs(hash(pid)) % 2**32)
    now = int(time.time() // 60 * 60)
    px = PRICES[pid]
    out = []
    for i in range(n):
        c = px * (1 + rng.normal(0, 0.001))
        out.append({"start": str(now - (n - i) * 60), "open": str(px), "high": str(max(px, c) * 1.0007),
                    "low": str(min(px, c) * 0.9993), "close": str(c), "volume": str(rng.uniform(5, 50))})
        px = c
    return out


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1e6):06d}Z"


@pytest.mark.asyncio
async def test_engine_end_to_end_offline(tmp_path, monkeypatch):
    settings = dataclasses.replace(
        SETTINGS, runtime=dataclasses.replace(SETTINGS.runtime, db_path=tmp_path / "e.db", mode="PAPER"))
    eng = engine_mod.TradingEngine(settings)
    gw = eng.gateway

    async def load_products(pids):
        for p in pids:
            gw.products[p] = ProductMeta(p, 1e-8, 0.01 if PRICES[p] > 10 else 0.0001, 1e-8, 1.0)
        return gw.products

    async def get_candles(pid, minutes):
        return _candles(pid, minutes)

    async def ping():
        return 12.0

    monkeypatch.setattr(gw, "load_products", load_products)
    monkeypatch.setattr(gw, "get_candles", get_candles)
    monkeypatch.setattr(gw, "ping", ping)

    def fake_ws(self):
        for name, ch in (("l2", ["level2"]), ("flow", ["market_trades", "ticker"])):
            c = engine_mod.WSConn(name, ch, list(settings.pairs), authenticated=False)
            c.connected, c.last_msg, c.last_heartbeat = True, time.time(), time.time()
            self.conns[name] = c

    monkeypatch.setattr(engine_mod.TradingEngine, "_start_market_ws", fake_ws)
    await eng.start()

    seq = {"l2": 0, "flow": 0}

    def push(conn, msg):
        seq[conn] += 1
        msg["sequence_num"] = seq[conn]
        msg["timestamp"] = _iso(time.time())
        eng._enqueue_ws(conn, msg, time.time())

    for pid, px in PRICES.items():
        tick = 0.01 if px > 10 else 0.0001
        ups = []
        for i in range(25):
            ups.append({"side": "bid", "price_level": f"{px - (i + 1) * tick:.4f}", "new_quantity": "8"})
            ups.append({"side": "offer", "price_level": f"{px + (i + 1) * tick:.4f}", "new_quantity": "2"})
        # Coinbase envía los pares USDC con el alias USD (liquidez unificada)
        push("l2", {"channel": "l2_data", "events": [{"type": "snapshot", "product_id": pid.replace("USDC", "USD"),
                                                     "updates": ups}]})

    # BTC: flujo vendedor agresivo + caída → debe activar el veto del oráculo
    for k in range(40):
        ts = time.time()
        trades = []
        for pid, px in PRICES.items():
            if pid == "BTC-USDC":
                p = px * (1 - 0.0002 * k)
                trades.append({"trade_id": str(k), "product_id": pid.replace("USDC", "USD"), "price": f"{p:.2f}", "size": "0.5",
                               "side": "SELL", "time": _iso(ts)})
            else:
                trades.append({"trade_id": str(k), "product_id": pid.replace("USDC", "USD"), "price": f"{px:.4f}", "size": "1",
                               "side": "BUY", "time": _iso(ts)})
        push("flow", {"channel": "market_trades", "events": [{"type": "update", "trades": trades}]})
    push("l2", {"channel": "heartbeats", "events": [{"heartbeat_counter": 1}]})
    push("flow", {"channel": "heartbeats", "events": [{"heartbeat_counter": 1}]})

    await asyncio.sleep(2.5)
    db = eng.db.sync
    snap = {r["product_id"]: r for r in db.query("SELECT * FROM pair_snapshot")}
    assert set(snap) == set(settings.pairs)
    assert all(r["price"] and r["spread_pct"] is not None for r in snap.values())
    assert snap["SOL-USDC"]["obi"] == pytest.approx(0.6)             # (8-2)/(8+2)
    assert eng.strategy.oracle.state.veto, eng.strategy.oracle.state
    kv = db.all_kv()
    assert kv["mode"] == "PAPER" and kv["bot_status"] in {"RUNNING", "SAFE"}
    assert not math.isnan(eng.strategy.analytics["ETH-USDC"].atr)     # régimen calculado sobre historia

    db.push_command("PAUSE", "test")
    await asyncio.sleep(0.6)
    assert eng.db.sync.get_kv("bot_status") == "PAUSED" or eng.risk.state.manual_paused
    db.push_command("PANIC", "test")
    db.push_command("RESUME", "test")      # PANIC se procesa antes aunque llegue a la vez
    await asyncio.sleep(1.0)
    cmds = {c["command"]: c for c in db.recent_commands(10)}
    assert cmds["PANIC"]["result"].startswith("OK")
    assert cmds["PANIC"]["processed_at"] <= cmds["RESUME"]["processed_at"]
    await eng.shutdown()
