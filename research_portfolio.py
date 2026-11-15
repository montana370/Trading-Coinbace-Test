"""
research_portfolio.py — Cartera multi-mercado del sistema de tendencia 6 h en perpetuos US.

Todos los perpetuos US líquidos de Coinbase, SIN descartar los que pierden (evita sesgo de
selección). Parámetros FIJOS (N=20, M=10, k=2.5) elegidos sólo con BTC/ETH/SOL/SUI.
Cartera con capital compartido, riesgo por operación r, máximo K posiciones simultáneas
y máximo D posiciones en la misma dirección (control de correlación).
Costes: maker 0.02 %, taker 0.05 %, financiación 0.01 %/8 h siempre; apalancamiento ≤ 2×.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import indicators as ind
from research_trend import fetch

MAKER, TAKER, FUND_8H, LEV = 0.0002, 0.0005, 0.0001, 2.0
SECS = 21600
N, M, K_ATR = 20, 10, 2.5
COINS = ["BTC", "ETH", "SOL", "SUI", "XRP", "DOGE", "AVAX", "LINK", "LTC", "ADA", "BCH", "DOT", "XLM",
         "HBAR", "NEAR", "AAVE", "ZEC", "BNB", "ONDO", "ENA", "SHIB", "PEPE"]


def load() -> dict[str, dict[str, np.ndarray]]:
    data = {}
    for c in COINS:
        pid = f"{c}-USDC" if c in ("BTC", "ETH", "SOL", "SUI") else f"{c}-USD"
        d = fetch(pid, "SIX_HOUR", SECS, 4.0)
        d["atr"] = ind.atr(d["h"], d["l"], d["c"], 14)
        d["ema"] = ind.ema(d["c"], 200)
        data[c] = d
    return data


def run(data: dict, r: float, k_max: int, d_max: int, t_from: float, t_to: float, eq0: float = 10_000.0) -> dict:
    idx = {c: {t: i for i, t in enumerate(d["t"])} for c, d in data.items()}
    times = sorted({t for d in data.values() for t in d["t"] if t_from <= t < t_to})
    eq, peak, mdd = eq0, eq0, 0.0
    pos: dict[str, list] = {}       # coin → [side, entry, size, stop, extreme]
    rets, curve = [], []
    fund = FUND_8H * SECS / 28800
    for t in times:
        # 1) gestionar posiciones abiertas con la vela de este instante
        for c in list(pos):
            i = idx[c].get(t)
            if i is None:
                continue
            d = data[c]
            side, entry, size, stop, ext = pos[c]
            eq -= entry * size * fund
            px = None
            if side > 0:
                if d["l"][i] <= stop:
                    px = min(stop, d["o"][i])
                elif d["c"][i] < np.min(d["l"][i - M:i]):
                    px = d["c"][i]
            else:
                if d["h"][i] >= stop:
                    px = max(stop, d["o"][i])
                elif d["c"][i] > np.max(d["h"][i - M:i]):
                    px = d["c"][i]
            if px is not None:
                pnl = (px - entry) * size * side - entry * size * MAKER - px * size * TAKER
                eq += pnl
                rets.append(pnl)
                del pos[c]
            elif side > 0:
                ext = max(ext, d["h"][i])
                pos[c] = [side, entry, size, max(stop, ext - K_ATR * d["atr"][i]), ext]
            else:
                ext = min(ext, d["l"][i])
                pos[c] = [side, entry, size, min(stop, ext + K_ATR * d["atr"][i]), ext]
        # 2) nuevas entradas (en orden aleatorio estable para no favorecer ninguna moneda)
        for c in sorted(data, key=lambda x: hash((x, t)) % 997):
            if c in pos or len(pos) >= k_max:
                continue
            i = idx[c].get(t)
            d = data[c]
            if i is None or i < 201 or i + 1 >= len(d["c"]) or np.isnan(d["ema"][i]):
                continue
            cl = d["c"][i]
            side = 0
            if cl > np.max(d["h"][i - N:i]) and cl > d["ema"][i]:
                side = 1
            elif cl < np.min(d["l"][i - N:i]) and cl < d["ema"][i]:
                side = -1
            if not side or sum(1 for p in pos.values() if p[0] == side) >= d_max:
                continue
            entry = d["o"][i + 1]
            stop = entry - side * K_ATR * d["atr"][i]
            risk_u = abs(entry - stop) + entry * MAKER + stop * TAKER
            notional_open = sum(p[1] * p[2] for p in pos.values())
            size = min(eq * r / risk_u, max(eq * LEV - notional_open, 0) / entry)
            if size * entry < 50:
                continue
            pos[c] = [side, entry, size, stop, entry]
        mark = eq + sum((data[c]["c"][idx[c][t]] - p[1]) * p[2] * p[0] for c, p in pos.items() if t in idx[c])
        peak = max(peak, mark)
        mdd = max(mdd, (peak - mark) / peak * 100)
        curve.append(mark)
    a = np.array(rets)
    days = (t_to - t_from) / 86400
    monthly = np.diff(np.array(curve)[:: int(30 * 86400 / SECS)]) if len(curve) > 240 else np.array([])
    return {"return_pct": round((eq / eq0 - 1) * 100, 1), "max_dd_pct": round(mdd, 1), "trades": len(a),
            "trades_per_day": round(len(a) / days, 2),
            "profit_factor": round(float(a[a > 0].sum() / -a[a < 0].sum()), 2) if (a < 0).any() else None,
            "win_rate": round(float((a > 0).mean() * 100), 1) if len(a) else None,
            "months_positive_pct": round(float((monthly > 0).mean() * 100), 0) if len(monthly) else None}


def main() -> None:
    data = load()
    t0 = max(d["t"][210] for d in data.values() if len(d["t"]) > 1000)
    t_end = max(d["t"][-1] for d in data.values())
    mid = t0 + (t_end - t0) / 2
    out = []
    for r, k, dmax in ((0.01, 2, 2), (0.005, 6, 4), (0.005, 10, 6), (0.0035, 12, 8), (0.01, 6, 4)):
        full = run(data, r, k, dmax, t0, t_end)
        last = run(data, r, k, dmax, mid, t_end)
        row = {"riesgo": r, "max_pos": k, "max_misma_dir": dmax, "todo": full, "ultima_mitad": last}
        out.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    Path("data/backtest").mkdir(parents=True, exist_ok=True)
    Path("data/backtest/portfolio_research.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
