"""
research_perps.py — ¿Hay ventaja operando LARGO y CORTO con comisiones de perpetuos?

Perpetuos US de Coinbase (Coinbase Financial Markets): BIP (BTC), ETP (ETH), SLP (SOL), SUP (SUI).
Supuestos CONSERVADORES:
    * comisión maker 0.02 %, taker 0.05 % (la entrada se asume maker, la salida taker)
    * financiación: coste fijo de 0.01 % cada 8 h, cargado SIEMPRE (también en cortos,
      que en la práctica suelen cobrarla) → penaliza a la estrategia.
    * precio: spot de Coinbase como aproximación del perpetuo (la base es pequeña).

Sistema simétrico (mismo que research_trend en largos, espejo en cortos):
    LARGO  si cierre > máx(N) y cierre > EMA200; trailing máx − k·ATR; salida si cierre < mín(M)
    CORTO  si cierre < mín(N) y cierre < EMA200; trailing mín + k·ATR; salida si cierre > máx(M)
Riesgo 1 % por operación, apalancamiento máximo 2× por par.
Walk-forward: parámetros elegidos en la primera mitad, evaluados en la segunda.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np

import indicators as ind
from config import SETTINGS
from research_trend import fetch

MAKER, TAKER = 0.0002, 0.0005
FUNDING_PER_8H = 0.0001
MAX_LEVERAGE = 2.0
TFS = {"SIX_HOUR": 21600, "ONE_DAY": 86400}


def simulate(d: dict[str, np.ndarray], secs: int, n_entry: int, n_exit: int, k_atr: float,
             i0: int, i1: int, sides: tuple[str, ...] = ("long", "short"), eq0: float = 10_000.0) -> dict:
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    atr = ind.atr(h, l, c, 14)
    ema = ind.ema(c, 200)
    fund_per_bar = FUNDING_PER_8H * secs / 28800
    eq, peak, mdd = eq0, eq0, 0.0
    trades: list[tuple[str, float]] = []
    pos = None  # [side, entry, size, stop, extreme]
    start = max(i0, 201, n_entry + 1)
    i = start
    for i in range(start, i1):
        if pos is not None:
            side, entry, size, stop, ext = pos
            eq -= entry * size * fund_per_bar                    # financiación (conservadora)
            exit_px = None
            if side == "long":
                if l[i] <= stop:
                    exit_px = min(stop, o[i])
                elif c[i] < np.min(l[i - n_exit:i]):
                    exit_px = c[i]
            else:
                if h[i] >= stop:
                    exit_px = max(stop, o[i])
                elif c[i] > np.max(h[i - n_exit:i]):
                    exit_px = c[i]
            if exit_px is not None:
                gross = (exit_px - entry) * size if side == "long" else (entry - exit_px) * size
                pnl = gross - entry * size * MAKER - exit_px * size * TAKER
                eq += pnl
                trades.append((side, pnl / (entry * size) * 100))
                pos = None
            elif side == "long":
                ext = max(ext, h[i])
                pos = [side, entry, size, max(stop, ext - k_atr * atr[i]), ext]
            else:
                ext = min(ext, l[i])
                pos = [side, entry, size, min(stop, ext + k_atr * atr[i]), ext]
        if pos is None and i + 1 < i1 and not np.isnan(ema[i]):
            side = None
            if "long" in sides and c[i] > np.max(h[i - n_entry:i]) and c[i] > ema[i]:
                side = "long"
            elif "short" in sides and c[i] < np.min(l[i - n_entry:i]) and c[i] < ema[i]:
                side = "short"
            if side:
                entry = o[i + 1]
                stop = entry - k_atr * atr[i] if side == "long" else entry + k_atr * atr[i]
                risk_u = abs(entry - stop) + entry * MAKER + stop * TAKER
                size = min(eq * 0.01 / risk_u, eq * MAX_LEVERAGE / entry)
                pos = [side, entry, size, stop, entry]
        if pos is not None:
            side, entry, size = pos[0], pos[1], pos[2]
            mark = eq + ((c[i] - entry) if side == "long" else (entry - c[i])) * size
        else:
            mark = eq
        peak = max(peak, mark)
        mdd = max(mdd, (peak - mark) / peak * 100)
    if pos is not None:
        side, entry, size, *_ = pos
        px = c[i1 - 1]
        gross = (px - entry) * size if side == "long" else (entry - px) * size
        eq += gross - entry * size * MAKER - px * size * TAKER
        trades.append((side, gross / (entry * size) * 100))
    r = np.array([t[1] for t in trades])
    wins, losses = r[r > 0], -r[r < 0]
    return {
        "trades": len(r), "longs": sum(1 for t in trades if t[0] == "long"),
        "shorts": sum(1 for t in trades if t[0] == "short"),
        "return_pct": round((eq / eq0 - 1) * 100, 2),
        "profit_factor": round(float(wins.sum() / losses.sum()), 2) if losses.sum() > 0 else None,
        "win_rate": round(len(wins) / len(r) * 100, 1) if len(r) else None,
        "max_dd_pct": round(mdd, 2),
        "bh_pct": round((c[i1 - 1] / c[start] - 1) * 100, 1),
    }


GRID = {"n_entry": [20, 40, 55], "n_exit": [10, 20], "k_atr": [2.5, 3.5, 5.0]}


def main() -> None:
    out: dict = {"supuestos": {"maker": MAKER, "taker": TAKER, "funding_8h": FUNDING_PER_8H,
                               "max_leverage": MAX_LEVERAGE, "riesgo_por_operacion": 0.01}}
    for gran, secs in TFS.items():
        data = {pid: fetch(pid, gran, secs, 4.0) for pid in SETTINGS.pairs}
        for label, sides in (("largo_y_corto", ("long", "short")), ("solo_largo", ("long",))):
            rows = []
            for vals in itertools.product(*GRID.values()):
                p = dict(zip(GRID, vals))
                sc = np.mean([
                    (m := simulate(d, secs, **p, i0=0, i1=len(d["c"]) // 2, sides=sides))["return_pct"]
                    - 0.5 * m["max_dd_pct"] for d in data.values()])
                rows.append((sc, p))
            rows.sort(key=lambda r: r[0], reverse=True)
            best = rows[0][1]
            res = {}
            for pid, d in data.items():
                half = len(d["c"]) // 2
                res[pid] = {"train": simulate(d, secs, **best, i0=0, i1=half, sides=sides),
                            "test": simulate(d, secs, **best, i0=half, i1=len(d["c"]), sides=sides)}
            tot_test = round(float(np.mean([v["test"]["return_pct"] for v in res.values()])), 2)
            out[f"{gran}_{label}"] = {"best": best, "test_medio_pct": tot_test,
                                      "combos_positivos_train_pct": round(sum(1 for s, _ in rows if s > 0) / len(rows) * 100, 1),
                                      "pares": res}
            print(gran, label, "mejor", best, "| prueba media", tot_test, "%", flush=True)
            for pid, v in res.items():
                print("   ", pid, "train", v["train"], "\n    ", " " * len(pid), "TEST ", v["test"], flush=True)
    Path("data/backtest").mkdir(parents=True, exist_ok=True)
    Path("data/backtest/perps_research.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
