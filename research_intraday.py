"""
research_intraday.py — ¿Existen estrategias INTRADÍA con ventaja en perpetuos de Coinbase?

Objetivo: operar a diario sólo si hay expectativa positiva demostrada fuera de muestra.
Costes (conservadores): entrada maker 0.02 %, salida taker 0.05 %, financiación 0.01 %/8 h
cobrada siempre. Precio: velas de Coinbase (spot ≈ perpetuo).

Estrategias (largo y corto, simétricas):
  A) RUPTURA  — Donchian N barras + filtro EMA(200); trailing k·ATR; salida canal M barras.
  B) REVERSIÓN — dentro de la tendencia (EMA 200): largo si RSI(2) < lo y cierre < BB inferior(20, z);
                 corto si RSI(2) > 100−lo y cierre > BB superior. Salida: cierre cruza la media
                 de 20, stop k·ATR, o tiempo máximo T barras.

Walk-forward: 60 % de los datos para elegir parámetros, 40 % final fuera de muestra.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np

import indicators as ind
from config import SETTINGS
from research_perps import simulate as breakout_sim
from research_trend import fetch

MAKER, TAKER = 0.0002, 0.0005
FUNDING_PER_8H = 0.0001
MAX_LEVERAGE = 2.0
RISK = 0.01


def reversion_sim(d: dict[str, np.ndarray], secs: int, lo: float, z: float, k_atr: float, t_max: int,
                  i0: int, i1: int, eq0: float = 10_000.0) -> dict:
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    atr = ind.atr(h, l, c, 14)
    ema = ind.ema(c, 200)
    r2 = ind.rsi(c, 2)
    mid, up, dn = ind.bollinger(c, 20, z)
    fund = FUNDING_PER_8H * secs / 28800
    eq, peak, mdd = eq0, eq0, 0.0
    rets: list[float] = []
    pos = None   # [side, entry, size, stop, bars]
    start = max(i0, 201)
    for i in range(start, i1):
        if pos is not None:
            side, entry, size, stop, bars = pos
            eq -= entry * size * fund
            bars += 1
            px = None
            if side > 0:
                if l[i] <= stop:
                    px = min(stop, o[i])
                elif c[i] >= mid[i] or bars >= t_max:
                    px = c[i]
            else:
                if h[i] >= stop:
                    px = max(stop, o[i])
                elif c[i] <= mid[i] or bars >= t_max:
                    px = c[i]
            if px is not None:
                pnl = (px - entry) * size * side - entry * size * MAKER - px * size * TAKER
                eq += pnl
                rets.append(pnl / (entry * size) * 100)
                pos = None
            else:
                pos[4] = bars
        if pos is None and i + 1 < i1 and not np.isnan(ema[i]) and not np.isnan(dn[i]):
            side = 0
            if c[i] > ema[i] and r2[i] < lo and c[i] < dn[i]:
                side = 1
            elif c[i] < ema[i] and r2[i] > 100 - lo and c[i] > up[i]:
                side = -1
            if side:
                # entrada maker a la apertura siguiente sólo si el precio vuelve a nuestro nivel
                entry = o[i + 1]
                stop = entry - side * k_atr * atr[i]
                risk_u = abs(entry - stop) + entry * MAKER + stop * TAKER
                size = min(eq * RISK / risk_u, eq * MAX_LEVERAGE / entry)
                pos = [side, entry, size, stop, 0]
        mark = eq + ((c[i] - pos[1]) * pos[2] * pos[0] if pos else 0.0)
        peak = max(peak, mark)
        mdd = max(mdd, (peak - mark) / peak * 100)
    r = np.array(rets)
    wins, losses = r[r > 0], -r[r < 0]
    days = (i1 - start) * secs / 86400
    return {"trades": len(r), "trades_per_day": round(len(r) / max(days, 1e-9), 2),
            "return_pct": round((eq / eq0 - 1) * 100, 2),
            "profit_factor": round(float(wins.sum() / losses.sum()), 2) if losses.sum() > 0 else None,
            "win_rate": round(len(wins) / len(r) * 100, 1) if len(r) else None, "max_dd_pct": round(mdd, 2)}


def evaluate(name: str, data: dict, secs: int, grid: dict, fn, split: float = 0.6) -> dict:
    rows = []
    for vals in itertools.product(*grid.values()):
        p = dict(zip(grid, vals))
        tr, te = [], []
        for d in data.values():
            n = len(d["c"])
            cut = int(n * split)
            tr.append(fn(d, secs, **p, i0=0, i1=cut))
            te.append(fn(d, secs, **p, i0=cut, i1=n))
        sc = np.mean([m["return_pct"] - 0.5 * m["max_dd_pct"] for m in tr])
        rows.append({"params": p, "score_train": round(float(sc), 2),
                     "test_ret": [m["return_pct"] for m in te], "test_pf": [m["profit_factor"] for m in te],
                     "test_trades": [m["trades"] for m in te], "test_dd": [m["max_dd_pct"] for m in te]})
    rows.sort(key=lambda r: r["score_train"], reverse=True)
    best = rows[0]
    n_test_days = (len(next(iter(data.values()))["c"]) * (1 - split)) * secs / 86400
    pos_oos = sum(1 for r in rows if np.mean(r["test_ret"]) > 0)
    out = {
        "strategy": name, "combos": len(rows), "oos_positive_combos": pos_oos,
        "best_by_train": best["params"],
        "best_test_return_by_pair": dict(zip(data.keys(), best["test_ret"])),
        "best_test_pf_by_pair": dict(zip(data.keys(), best["test_pf"])),
        "best_test_trades_per_day_portfolio": round(sum(best["test_trades"]) / n_test_days, 2),
        "best_test_max_dd": max(best["test_dd"]),
        "test_days": round(n_test_days),
    }
    print(json.dumps(out, ensure_ascii=False, default=float), flush=True)
    return out


def main() -> None:
    results = []
    for gran, secs, years in (("ONE_HOUR", 3600, 4.0), ("FIFTEEN_MINUTE", 900, 1.5)):
        data = {pid: fetch(pid, gran, secs, years) for pid in SETTINGS.pairs}
        results.append(evaluate(f"RUPTURA {gran}", data, secs,
                                {"n_entry": [20, 48, 96], "n_exit": [10, 24], "k_atr": [2.0, 3.0, 4.0]},
                                breakout_sim))
        results.append(evaluate(f"REVERSIÓN {gran}", data, secs,
                                {"lo": [5.0, 10.0], "z": [2.0, 2.5], "k_atr": [2.0, 3.0], "t_max": [12, 24]},
                                reversion_sim))
    Path("data/backtest").mkdir(parents=True, exist_ok=True)
    Path("data/backtest/intraday_research.json").write_text(json.dumps(results, indent=1, default=float))


if __name__ == "__main__":
    main()
