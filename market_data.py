"""
market_data.py — Estado de mercado en memoria por par.

* OrderBook L2 incremental (canal `level2`, mensajes `l2_data`).
* Flujo de órdenes (canal `market_trades`) → CVD cumulativo y por ventana.
* Barras OHLCV de 1m construidas desde trades (sembradas con REST candles).
* VWAP de sesión (UTC) con bandas de desviación estándar ponderada por volumen.
"""
from __future__ import annotations

import heapq
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from indicators import order_book_imbalance


def parse_ts(value: str | float | int | None) -> float:
    """Convierte timestamps RFC3339 de Coinbase (con nanosegundos) a epoch."""
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "." in s:
        head, frac = s.split(".", 1)
        frac_digits = "".join(ch for ch in frac if ch.isdigit())[:6]
        s = f"{head}.{frac_digits.ljust(6, '0')}"
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return time.time()


class OrderBook:
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.initialized = False
        self.last_update = 0.0
        self._best_bid: float | None = None
        self._best_ask: float | None = None
        self._dirty = True

    def apply_snapshot(self, updates: list[dict]) -> None:
        self.bids.clear()
        self.asks.clear()
        self._apply(updates)
        self.initialized = True

    def apply_updates(self, updates: list[dict]) -> None:
        if not self.initialized:
            return
        self._apply(updates)

    def _apply(self, updates: list[dict]) -> None:
        for u in updates:
            price = float(u["price_level"])
            qty = float(u["new_quantity"])
            book = self.bids if u["side"] == "bid" else self.asks
            if qty <= 0.0:
                book.pop(price, None)
            else:
                book[price] = qty
        self.last_update = time.time()
        self._dirty = True

    def _refresh(self) -> None:
        if self._dirty:
            self._best_bid = max(self.bids) if self.bids else None
            self._best_ask = min(self.asks) if self.asks else None
            self._dirty = False

    @property
    def best_bid(self) -> float | None:
        self._refresh()
        return self._best_bid

    @property
    def best_ask(self) -> float | None:
        self._refresh()
        return self._best_ask

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread_pct(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None or bb <= 0:
            return None
        return (ba - bb) / ((ba + bb) / 2.0) * 100.0

    def top(self, n: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = heapq.nlargest(n, self.bids.items(), key=lambda kv: kv[0])
        asks = heapq.nsmallest(n, self.asks.items(), key=lambda kv: kv[0])
        return bids, asks

    def obi(self, levels: int) -> float:
        bids, asks = self.top(levels)
        return order_book_imbalance(
            np.array([s for _, s in bids], dtype=float), np.array([s for _, s in asks], dtype=float)
        )

    def ask_walls(self, levels: int, multiple: float) -> list[tuple[float, float]]:
        """Niveles de venta con tamaño > multiple × mediana (zonas de liquidez)."""
        _, asks = self.top(levels)
        if len(asks) < 5:
            return []
        med = float(np.median([s for _, s in asks]))
        return [(p, s) for p, s in asks if med > 0 and s >= multiple * med]

    def bid_wall_below(self, levels: int, multiple: float) -> list[tuple[float, float]]:
        bids, _ = self.top(levels)
        if len(bids) < 5:
            return []
        med = float(np.median([s for _, s in bids]))
        return [(p, s) for p, s in bids if med > 0 and s >= multiple * med]


@dataclass
class Bar:
    start: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0


@dataclass
class TradeTick:
    ts: float
    price: float
    size: float
    signed: float   # +size compra agresora, -size venta agresora


@dataclass
class SessionVWAP:
    day: int = -1
    pv: float = 0.0
    v: float = 0.0
    p2v: float = 0.0

    def update(self, ts: float, price: float, size: float) -> None:
        day = int(ts // 86400)
        if day != self.day:
            self.day, self.pv, self.v, self.p2v = day, 0.0, 0.0, 0.0
        self.pv += price * size
        self.v += size
        self.p2v += price * price * size

    @property
    def vwap(self) -> float:
        return self.pv / self.v if self.v > 0 else float("nan")

    @property
    def sigma(self) -> float:
        if self.v <= 0:
            return float("nan")
        var = self.p2v / self.v - self.vwap**2
        return math.sqrt(max(var, 0.0))


@dataclass
class MarketState:
    product_id: str
    max_bars: int = 1500
    book: OrderBook = field(init=False)
    bars: deque[Bar] = field(init=False)
    trades: deque[TradeTick] = field(default_factory=lambda: deque(maxlen=50_000))
    vwap: SessionVWAP = field(default_factory=SessionVWAP)
    cvd_total: float = 0.0
    last_price: float = float("nan")
    last_trade_ts: float = 0.0
    ticker_ts: float = 0.0
    volume_24h: float = 0.0
    base_increment: float = 1e-8
    quote_increment: float = 0.01
    base_min_size: float = 0.0

    def __post_init__(self) -> None:
        self.book = OrderBook(self.product_id)
        self.bars = deque(maxlen=self.max_bars)

    # ── ingesta ─────────────────────────────────────────────────────────────
    def seed_candles(self, candles: list[dict]) -> None:
        """Siembra con velas REST (ONE_MINUTE). Sin split comprador/vendedor."""
        rows = sorted(candles, key=lambda c: int(c["start"]))
        for c in rows:
            b = Bar(
                start=float(c["start"]),
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c["volume"]),
            )
            if self.bars and b.start <= self.bars[-1].start:
                continue
            self.bars.append(b)
            # VWAP de sesión aproximado con precio típico de la vela del día actual
            typical = (b.high + b.low + b.close) / 3.0
            if int(b.start // 86400) == int(time.time() // 86400):
                self.vwap.update(b.start, typical, b.volume)
        if self.bars:
            self.last_price = self.bars[-1].close

    def on_trade(self, ts: float, price: float, size: float, aggressor_buy: bool) -> None:
        signed = size if aggressor_buy else -size
        self.trades.append(TradeTick(ts, price, size, signed))
        self.cvd_total += signed
        self.last_price = price
        self.last_trade_ts = ts
        self.vwap.update(ts, price, size)

        bucket = math.floor(ts / 60.0) * 60.0
        if not self.bars or bucket > self.bars[-1].start:
            prev_close = self.bars[-1].close if self.bars else price
            # rellena minutos sin trades para mantener la serie continua
            if self.bars:
                gap_start = self.bars[-1].start + 60.0
                while gap_start < bucket:
                    self.bars.append(Bar(gap_start, prev_close, prev_close, prev_close, prev_close))
                    gap_start += 60.0
            self.bars.append(Bar(bucket, price, price, price, price))
        bar = self.bars[-1]
        if bucket < bar.start:  # trade tardío: se ignora para las barras
            return
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.volume += size
        if aggressor_buy:
            bar.buy_volume += size
        else:
            bar.sell_volume += size

    # ── consultas de flujo ──────────────────────────────────────────────────
    def window_trades(self, seconds: float, now: float | None = None) -> list[TradeTick]:
        now = now or time.time()
        cutoff = now - seconds
        out: list[TradeTick] = []
        for t in reversed(self.trades):
            if t.ts < cutoff:
                break
            out.append(t)
        out.reverse()
        return out

    def cvd_window(self, seconds: float) -> tuple[float, float]:
        """(delta neto, volumen total) en la ventana."""
        ts = self.window_trades(seconds)
        delta = sum(t.signed for t in ts)
        vol = sum(t.size for t in ts)
        return delta, vol

    def price_change_pct(self, seconds: float) -> float:
        ts = self.window_trades(seconds)
        if len(ts) < 2 or ts[0].price <= 0:
            # fallback a barras
            n = max(int(seconds // 60), 1)
            if len(self.bars) > n:
                ref = self.bars[-n - 1].close
                return (self.bars[-1].close - ref) / ref * 100.0 if ref > 0 else 0.0
            return 0.0
        return (ts[-1].price - ts[0].price) / ts[0].price * 100.0

    # ── series de barras ────────────────────────────────────────────────────
    def series(self, tf_minutes: int = 1, include_partial: bool = False) -> dict[str, np.ndarray]:
        bars = list(self.bars)
        if not include_partial and bars and time.time() < bars[-1].start + 60.0:
            bars = bars[:-1]
        if tf_minutes > 1:
            bars = self._resample(bars, tf_minutes)
        return {
            "start": np.array([b.start for b in bars], dtype=float),
            "open": np.array([b.open for b in bars], dtype=float),
            "high": np.array([b.high for b in bars], dtype=float),
            "low": np.array([b.low for b in bars], dtype=float),
            "close": np.array([b.close for b in bars], dtype=float),
            "volume": np.array([b.volume for b in bars], dtype=float),
            "buy_volume": np.array([b.buy_volume for b in bars], dtype=float),
            "sell_volume": np.array([b.sell_volume for b in bars], dtype=float),
        }

    @staticmethod
    def _resample(bars: list[Bar], tf: int) -> list[Bar]:
        out: list[Bar] = []
        span = tf * 60.0
        for b in bars:
            bucket = math.floor(b.start / span) * span
            if not out or out[-1].start != bucket:
                out.append(Bar(bucket, b.open, b.high, b.low, b.close, b.volume, b.buy_volume, b.sell_volume))
            else:
                o = out[-1]
                o.high = max(o.high, b.high)
                o.low = min(o.low, b.low)
                o.close = b.close
                o.volume += b.volume
                o.buy_volume += b.buy_volume
                o.sell_volume += b.sell_volume
        # descarta la última barra de TF superior si está incompleta
        if out and bars and bars[-1].start < out[-1].start + span - 60.0:
            out = out[:-1]
        return out

    @property
    def is_ready(self) -> bool:
        return self.book.initialized and not math.isnan(self.last_price)


class SequenceTracker:
    """Detecta huecos en `sequence_num` por conexión WebSocket."""

    def __init__(self) -> None:
        self.last: int | None = None
        self.gaps = 0

    def check(self, seq: int | None) -> bool:
        """False sólo si hay un hueco hacia adelante (mensajes perdidos)."""
        if seq is None:
            return True
        gap = self.last is not None and seq > self.last + 1
        if gap:
            self.gaps += 1
        if self.last is None or seq > self.last:
            self.last = seq
        return not gap

    def reset(self) -> None:
        self.last = None
