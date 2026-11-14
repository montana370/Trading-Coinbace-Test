"""
research_trend.py — Investigación: seguimiento de tendencia en temporalidades altas.

Hipótesis: con comisiones minoristas de Coinbase (≈1 % ida y vuelta), sólo
estrategias con recorridos grandes y pocas operaciones pueden tener expectativa
positiva. Se evalúa un sistema clásico y con pocos parámetros:

    * Entrada LONG: cierre > máximo de las N barras anteriores (Donchian)
                    y cierre > EMA(200) (filtro de tendencia).
    * Salida: trailing stop = máximo desde la entrada − k·ATR(14), o cierre <
              mínimo de M barras. Sin TP fijo: se deja correr la tendencia.
    * Comisiones: entrada maker 0.40 %, salida taker 0.60 % (conservador).
    * Sizing: 1 % de riesgo por operación sobre la distancia inicial al stop,
              tope de asignación por par (config.py).

Walk-forward: parámetros elegidos SOLO en la primera mitad, evaluados en la
segunda (fuera de muestra). Se compara con comprar y mantener.

Uso:  python research_trend.py
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import httpx
import numpy as np

import indicators as ind
from config import SETTINGS

URL = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"
GRAN = {360: ("SIX_HOUR", 21600), 1440: ("ONE_DAY", 86400)}
CACHE = Path("data/candles")
MAKER, TAKER = SETTINGS.fees.maker_fee, SETTINGS.fees.taker_fee


def fetch(pid: str, gran: str, secs: int, years: float) -> dict[str, np.ndarray]:
    path = CACHE / f"{pid}_{gran}.npz"
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        with np.load(path) as z:
            return {k: z[k] for k in z.files}
    end = int(time.time() // secs * secs)
    start = end - int(years * 365 * 86400)
    rows: dict[float, list[float]] = {}
    with httpx.Client(timeout=20) as c:
        cur = start
        while cur < end:
            ce = min(cur + 300 * secs, end)
            for attempt in range(6):
                r = c.get(URL.format(pid=pid), params={"start": cur, "end": ce, "granularity": gran})
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                break
            for k in r.json().get("candles", []):
                rows[float(k["start"])] = [float(k[x]) for x in ("start", "open", "high", "low", "close", "volume")]
            cur = ce
            time.sleep(0.15)
    a = np.array([rows[t] for t in sorted(rows)])
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, t=a[:, 0], o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4], v=a[:, 5])
    return {"t": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def simulate(d: dict[str, np.ndarray], n_entry: int, n_exit: int, k_atr: float, alloc: float,
             i0: int, i1: int, equity0: float = 10_000.0) -> dict[str, float]:
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    atr = ind.atr(h, l, c, 14)
    ema = ind.ema(c, 200)
    equity = equity0
    peak, max_dd = equity, 0.0
    trades: list[float] = []
    pos = None   # (entry, size, stop, highest)
    for i in range(max(i0, 201, n_entry + 1), i1):
        if pos is not None:
            entry, size, stop, highest = pos
            # salida: stop intrabar (taker) o cierre bajo mínimo de M barras
            exit_px = None
            if l[i] <= stop:
                exit_px = min(stop, o[i])
            elif c[i] < np.min(l[i - n_exit:i]):
                exit_px = c[i]
            if exit_px is not None:
                pnl = (exit_px - entry) * size - entry * size * MAKER - exit_px * size * TAKER
                equity += pnl
                trades.append(pnl / (entry * size) * 100)
                pos = None
            else:
                highest = max(highest, h[i])
                pos = (entry, size, max(stop, highest - k_atr * atr[i]), highest)
        if pos is None and not np.isnan(ema[i]) and c[i] > np.max(h[i - n_entry:i]) and c[i] > ema[i]:
            if i + 1 >= i1:
                break
            entry = o[i + 1]                     # siguiente apertura (orden límite al bid ≈ apertura)
            stop = entry - k_atr * atr[i]
            risk_u = (entry - stop) + entry * MAKER + stop * TAKER
            size = min(equity * 0.01 / risk_u, equity * alloc / entry)
            pos = (entry, size, stop, entry)
        mark = equity + ((c[i] - pos[0]) * pos[1] if pos else 0.0)
        peak = max(peak, mark)
        max_dd = max(max_dd, (peak - mark) / peak * 100)
    if pos is not None:
        entry, size, *_ = pos
        pnl = (c[i1 - 1] - entry) * size - entry * size * MAKER - c[i1 - 1] * size * TAKER
        equity += pnl
        trades.append(pnl / (entry * size) * 100)
    t = np.array(trades)
    wins, losses = t[t > 0], -t[t < 0]
    return {
        "trades": len(t), "return_pct": round((equity / equity0 - 1) * 100, 2),
        "win_rate": round(len(wins) / len(t) * 100, 1) if len(t) else None,
        "profit_factor": round(wins.sum() / losses.sum(), 2) if losses.sum() > 0 else None,
        "avg_trade_pct": round(float(t.mean()), 2) if len(t) else None,
        "max_dd_pct": round(max_dd, 2),
        "bh_pct": round((c[i1 - 1] / c[max(i0, 201)] - 1) * 100, 1),
    }


GRID = {"n_entry": [20, 40, 55], "n_exit": [10, 20], "k_atr": [2.5, 3.5, 5.0]}


def main() -> None:
    out: dict[str, object] = {}
    for tf, (gran, secs) in GRAN.items():
        data = {pid: fetch(pid, gran, secs, 4.0) for pid in SETTINGS.pairs}
        rows = []
        for vals in itertools.product(*GRID.values()):
            p = dict(zip(GRID, vals))
            tot_train = []
            for pid, d in data.items():
                half = len(d["c"]) // 2
                tot_train.append(simulate(d, **p, alloc=SETTINGS.pairs[pid].max_allocation, i0=0, i1=half))
            score = np.mean([m["return_pct"] - 0.5 * m["max_dd_pct"] for m in tot_train])
            rows.append((score, p))
        rows.sort(key=lambda r: r[0], reverse=True)
        best = rows[0][1]
        res = {}
        for pid, d in data.items():
            half = len(d["c"]) // 2
            a = SETTINGS.pairs[pid].max_allocation
            res[pid] = {
                "bars": len(d["c"]),
                "desde": time.strftime("%Y-%m-%d", time.gmtime(d["t"][0])),
                "train": simulate(d, **best, alloc=a, i0=0, i1=half),
                "test": simulate(d, **best, alloc=a, i0=half, i1=len(d["c"])),
                "test_params_medianos": simulate(d, n_entry=40, n_exit=20, k_atr=3.5, alloc=a, i0=half, i1=len(d["c"])),
            }
        out[gran] = {"best_params_train": best, "pairs": res,
                     "train_positive_share": round(sum(1 for s, _ in rows if s > 0) / len(rows) * 100, 1)}
        print(gran, json.dumps(out[gran], ensure_ascii=False), flush=True)
    Path("data/backtest").mkdir(parents=True, exist_ok=True)
    Path("data/backtest/trend_research.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
