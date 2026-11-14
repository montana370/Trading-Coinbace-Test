"""
history.py — Descarga y caché de velas históricas de 1 minuto (API pública de Coinbase).

Uso:
    python history.py --days 60                  # los 4 pares del universo
    python history.py --days 30 --pairs SOL-USDC

Se guardan en data/candles/{PAR}_1m.npz (columnas: start, open, high, low, close, volume)
y las descargas son incrementales: sólo se piden los minutos que faltan.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import numpy as np

from config import PRODUCT_IDS

URL = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"
CACHE = Path("data/candles")
CHUNK = 300 * 60   # 300 velas de 1 minuto por petición (límite API = 350)


def cache_path(pid: str) -> Path:
    return CACHE / f"{pid}_1m.npz"


def load(pid: str) -> dict[str, np.ndarray]:
    p = cache_path(pid)
    if not p.exists():
        raise FileNotFoundError(f"Sin histórico para {pid}: ejecuta `python history.py`")
    with np.load(p) as z:
        return {k: z[k] for k in z.files}


def _fetch(client: httpx.Client, pid: str, start: int, end: int) -> list[list[float]]:
    for attempt in range(8):
        try:
            r = client.get(URL.format(pid=pid), params={"start": start, "end": end, "granularity": "ONE_MINUTE"})
            if r.status_code == 429:
                time.sleep(1.5 * 2 ** attempt)
                continue
            r.raise_for_status()
            return [[float(c["start"]), float(c["open"]), float(c["high"]), float(c["low"]),
                     float(c["close"]), float(c["volume"])] for c in r.json().get("candles", [])]
        except httpx.HTTPError:
            if attempt == 7:
                raise
            time.sleep(1.0 * 2 ** attempt)
    return []


def download(pid: str, days: int, client: httpx.Client) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    end = int(time.time() // 60 * 60)
    start = end - days * 86400
    rows: dict[float, list[float]] = {}
    if cache_path(pid).exists():
        old = load(pid)
        for i in range(len(old["start"])):
            if old["start"][i] >= start:
                rows[old["start"][i]] = [old[k][i] for k in ("start", "open", "high", "low", "close", "volume")]
    have = set(rows)
    cursor = start
    n_req = 0
    while cursor < end:
        chunk_end = min(cursor + CHUNK, end)
        expected = range(cursor, chunk_end, 60)
        missing = sum(1 for t in expected if float(t) not in have)
        if missing > 5:   # minutos sin negociación no devuelven vela: tolerancia pequeña
            for row in _fetch(client, pid, cursor, chunk_end):
                rows[row[0]] = row
            n_req += 1
            time.sleep(0.12)
        cursor = chunk_end
    ordered = [rows[t] for t in sorted(rows)]
    arr = np.array(ordered, dtype=float)
    np.savez_compressed(cache_path(pid), start=arr[:, 0], open=arr[:, 1], high=arr[:, 2],
                        low=arr[:, 3], close=arr[:, 4], volume=arr[:, 5])
    return len(ordered)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--pairs", nargs="*", default=PRODUCT_IDS)
    args = ap.parse_args()
    with httpx.Client(timeout=20.0, headers={"User-Agent": "quant-terminal-backtest/1.0"}) as client:
        for pid in args.pairs:
            t0 = time.time()
            n = download(pid, args.days, client)
            print(f"{pid}: {n} velas 1m ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
