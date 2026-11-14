"""
exchange.py — Adaptadores de exchange.

* `CoinbaseGateway` (LIVE): envuelve `coinbase.rest.RESTClient` (síncrono) con
  `asyncio.to_thread`, mide latencia de cada llamada y normaliza respuestas.
* `PaperGateway` (PAPER): simulador de matching realista que consume el MISMO
  flujo de WebSocket en vivo: órdenes Post-Only con posición en cola L2,
  rechazo si cruzan el spread, ejecución a mercado recorriendo el libro y
  comisiones Maker/Taker reales.

Ambos implementan la interfaz `Gateway` y emiten `OrderUpdate` normalizados
hacia `on_order_update`, igual que el canal `user` de Coinbase.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Awaitable, Callable

from market_data import MarketState
from models import OrderStatus, OrderUpdate, Side

log = logging.getLogger("exchange")

OrderCallback = Callable[[OrderUpdate], Awaitable[None]]


class ExchangeError(Exception):
    pass


class PostOnlyRejected(ExchangeError):
    """La orden Post-Only habría cruzado el spread (sería Taker)."""


@dataclass
class ProductMeta:
    product_id: str
    base_increment: float
    quote_increment: float
    base_min_size: float
    quote_min_size: float


def to_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    return dict(getattr(resp, "__dict__", {}))


def round_step(value: float, step: float, up: bool = False) -> float:
    if step <= 0:
        return value
    d_val, d_step = Decimal(str(value)), Decimal(str(step))
    q = (d_val / d_step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return float(q * d_step)


def fmt_step(value: float, step: float) -> str:
    decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)  # type: ignore[operator]
    return f"{value:.{decimals}f}"


def map_status(raw: str) -> OrderStatus:
    raw = (raw or "").upper()
    return {
        "PENDING": OrderStatus.PENDING,
        "OPEN": OrderStatus.OPEN,
        "FILLED": OrderStatus.FILLED,
        "CANCELLED": OrderStatus.CANCELLED,
        "CANCEL_QUEUED": OrderStatus.OPEN,
        "EXPIRED": OrderStatus.EXPIRED,
        "FAILED": OrderStatus.FAILED,
        "REJECTED": OrderStatus.REJECTED,
    }.get(raw, OrderStatus.OPEN)


def new_client_order_id(prefix: str, tag: str) -> str:
    return f"{prefix}-{tag}-{uuid.uuid4().hex[:16]}"


class Gateway(ABC):
    mode: str

    def __init__(self) -> None:
        self.on_order_update: OrderCallback | None = None
        self.last_latency_ms: float = 0.0
        self.products: dict[str, ProductMeta] = {}

    async def _emit(self, upd: OrderUpdate) -> None:
        if self.on_order_update is not None:
            await self.on_order_update(upd)

    @abstractmethod
    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]: ...

    @abstractmethod
    async def get_candles(self, product_id: str, minutes: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_balances(self) -> dict[str, float]: ...

    @abstractmethod
    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str: ...

    @abstractmethod
    async def place_market_sell(self, product_id: str, base_size: float, client_order_id: str) -> str: ...

    @abstractmethod
    async def place_bracket_sell(
        self, product_id: str, base_size: float, limit_price: float, stop_trigger: float, client_order_id: str
    ) -> str:
        """Venta bracket GTC: límite de TP + stop de protección residente en el exchange."""

    @abstractmethod
    async def cancel(self, order_ids: list[str]) -> None: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> OrderUpdate | None: ...

    @abstractmethod
    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]: ...

    @abstractmethod
    async def ping(self) -> float: ...

    @abstractmethod
    async def fee_rates(self) -> tuple[float, float] | None: ...

    def meta(self, product_id: str) -> ProductMeta:
        return self.products[product_id]


# ═════════════════════════════════════════════════════════════════════════════
# LIVE — Coinbase Advanced Trade v3
# ═════════════════════════════════════════════════════════════════════════════
class CoinbaseGateway(Gateway):
    mode = "LIVE"

    def __init__(self, api_key: str, api_secret: str, timeout: int = 10) -> None:
        super().__init__()
        from coinbase.rest import RESTClient

        self.client = RESTClient(api_key=api_key, api_secret=api_secret, timeout=timeout)

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        return to_dict(resp)

    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]:
        for pid in product_ids:
            p = await self._call(self.client.get_product, pid)
            self.products[pid] = ProductMeta(
                product_id=pid,
                base_increment=float(p["base_increment"]),
                quote_increment=float(p.get("price_increment") or p["quote_increment"]),
                base_min_size=float(p["base_min_size"]),
                quote_min_size=float(p.get("quote_min_size") or 1.0),
            )
        return self.products

    async def get_candles(self, product_id: str, minutes: int) -> list[dict[str, Any]]:
        return await _fetch_candles(self._call, self.client.get_candles, product_id, minutes)

    async def get_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        cursor: str | None = None
        while True:
            resp = await self._call(self.client.get_accounts, limit=250, cursor=cursor)
            for acc in resp.get("accounts", []):
                cur = acc["currency"]
                avail = float(acc["available_balance"]["value"])
                hold = float((acc.get("hold") or {}).get("value", 0.0))
                out[cur] = out.get(cur, 0.0) + avail + hold
            if not resp.get("has_next"):
                break
            cursor = resp.get("cursor")
        return out

    def _parse_create(self, resp: dict[str, Any]) -> str:
        if resp.get("success"):
            return str(to_dict(resp["success_response"])["order_id"])
        err = to_dict(resp.get("error_response"))
        reason = f"{err.get('error')} {err.get('message')} {err.get('preview_failure_reason')}"
        if "POST_ONLY" in reason.upper():
            raise PostOnlyRejected(reason)
        raise ExchangeError(reason)

    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str:
        m = self.meta(product_id)
        size_s = fmt_step(round_step(base_size, m.base_increment), m.base_increment)
        price_s = fmt_step(round_step(price, m.quote_increment, up=side is Side.SELL), m.quote_increment)
        fn = self.client.limit_order_gtc_buy if side is Side.BUY else self.client.limit_order_gtc_sell
        resp = await self._call(
            fn, client_order_id=client_order_id, product_id=product_id,
            base_size=size_s, limit_price=price_s, post_only=True,
        )
        return self._parse_create(resp)

    async def place_bracket_sell(
        self, product_id: str, base_size: float, limit_price: float, stop_trigger: float, client_order_id: str
    ) -> str:
        m = self.meta(product_id)
        resp = await self._call(
            self.client.trigger_bracket_order_gtc_sell, client_order_id=client_order_id, product_id=product_id,
            base_size=fmt_step(round_step(base_size, m.base_increment), m.base_increment),
            limit_price=fmt_step(round_step(limit_price, m.quote_increment, up=True), m.quote_increment),
            stop_trigger_price=fmt_step(round_step(stop_trigger, m.quote_increment), m.quote_increment),
        )
        return self._parse_create(resp)

    async def place_market_sell(self, product_id: str, base_size: float, client_order_id: str) -> str:
        m = self.meta(product_id)
        size_s = fmt_step(round_step(base_size, m.base_increment), m.base_increment)
        resp = await self._call(
            self.client.market_order_sell, client_order_id=client_order_id,
            product_id=product_id, base_size=size_s,
        )
        return self._parse_create(resp)

    async def cancel(self, order_ids: list[str]) -> None:
        if not order_ids:
            return
        for i in range(0, len(order_ids), 100):  # límite del endpoint batch_cancel
            await self._call(self.client.cancel_orders, order_ids=order_ids[i:i + 100])

    @staticmethod
    def _to_update(o: dict[str, Any]) -> OrderUpdate:
        cfg = to_dict(o.get("order_configuration"))
        limit_price = 0.0
        for v in cfg.values():
            v = to_dict(v)
            if "limit_price" in v:
                limit_price = float(v["limit_price"])
        filled = float(o.get("filled_size") or 0.0)
        return OrderUpdate(
            order_id=o["order_id"],
            client_order_id=o.get("client_order_id", ""),
            product_id=o["product_id"],
            side=Side(o["side"]),
            status=map_status(o.get("status", "")),
            filled_size=filled,
            avg_price=float(o.get("average_filled_price") or 0.0),
            total_fees=float(o.get("total_fees") or 0.0),
            leaves_size=max(float(o.get("leaves_quantity") or 0.0), 0.0),
            limit_price=limit_price,
        )

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        resp = await self._call(self.client.get_order, order_id)
        o = to_dict(resp.get("order"))
        return self._to_update(o) if o else None

    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]:
        resp = await self._call(self.client.list_orders, product_ids=product_ids, order_status=["OPEN"])
        return [self._to_update(to_dict(o)) for o in resp.get("orders", [])]

    async def ping(self) -> float:
        await self._call(self.client.get_unix_time)
        return self.last_latency_ms

    async def fee_rates(self) -> tuple[float, float] | None:
        resp = await self._call(self.client.get_transaction_summary)
        tier = to_dict(resp.get("fee_tier"))
        if not tier:
            return None
        return float(tier["maker_fee_rate"]), float(tier["taker_fee_rate"])


async def _fetch_candles(
    caller: Callable[..., Awaitable[dict[str, Any]]], fn: Callable[..., Any], product_id: str, minutes: int
) -> list[dict[str, Any]]:
    """Descarga velas ONE_MINUTE paginando de 300 en 300 (límite API = 350)."""
    end = datetime.now(timezone.utc)
    start_limit = end - timedelta(minutes=minutes)
    out: list[dict[str, Any]] = []
    cursor_end = end
    while cursor_end > start_limit:
        chunk_start = max(cursor_end - timedelta(minutes=300), start_limit)
        for attempt in range(6):
            try:
                resp = await caller(
                    fn, product_id=product_id,
                    start=str(int(chunk_start.timestamp())), end=str(int(cursor_end.timestamp())),
                    granularity="ONE_MINUTE",
                )
                break
            except Exception as exc:  # noqa: BLE001
                if "429" not in str(exc) or attempt == 5:
                    raise
                await asyncio.sleep(1.5 * (2 ** attempt))   # rate limit: backoff exponencial
        out.extend(to_dict(c) for c in resp.get("candles", []))
        cursor_end = chunk_start
        await asyncio.sleep(0.35)   # < 3 req/s: bajo el límite de endpoints públicos
    return out


# ═════════════════════════════════════════════════════════════════════════════
# PAPER — simulador con matching sobre el libro real
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class _PaperOrder:
    order_id: str
    client_order_id: str
    product_id: str
    side: Side
    price: float
    size: float
    filled: float = 0.0
    notional: float = 0.0
    fees: float = 0.0
    queue_ahead: float = 0.0
    stop_trigger: float = 0.0          # >0 ⇒ orden bracket
    status: OrderStatus = OrderStatus.OPEN

    @property
    def leaves(self) -> float:
        return max(self.size - self.filled, 0.0)

    @property
    def avg(self) -> float:
        return self.notional / self.filled if self.filled > 0 else 0.0


class PaperGateway(Gateway):
    mode = "PAPER"

    def __init__(
        self,
        markets: dict[str, MarketState],
        starting_equity: float,
        maker_fee: float,
        taker_fee: float,
        quote: str = "USDC",
    ) -> None:
        super().__init__()
        from coinbase.rest import RESTClient

        self.public = RESTClient(api_key=None, api_secret=None)  # endpoints públicos
        self.markets = markets
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.quote = quote
        self.balances: dict[str, float] = {quote: starting_equity}
        self.orders: dict[str, _PaperOrder] = {}

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        return to_dict(resp)

    def restore_balances(self, balances: dict[str, float]) -> None:
        if balances:
            self.balances = {k: float(v) for k, v in balances.items()}

    async def load_products(self, product_ids: list[str]) -> dict[str, ProductMeta]:
        for pid in product_ids:
            p = await self._call(self.public.get_public_product, pid)
            self.products[pid] = ProductMeta(
                product_id=pid,
                base_increment=float(p["base_increment"]),
                quote_increment=float(p.get("price_increment") or p["quote_increment"]),
                base_min_size=float(p["base_min_size"]),
                quote_min_size=float(p.get("quote_min_size") or 1.0),
            )
        return self.products

    async def get_candles(self, product_id: str, minutes: int) -> list[dict[str, Any]]:
        return await _fetch_candles(self._call, self.public.get_public_candles, product_id, minutes)

    async def get_balances(self) -> dict[str, float]:
        return dict(self.balances)

    def _available(self, currency: str) -> float:
        held = 0.0
        for o in self.orders.values():
            if o.status is not OrderStatus.OPEN:
                continue
            base = o.product_id.split("-")[0]
            if o.side is Side.BUY and currency == self.quote:
                held += o.leaves * o.price * (1 + self.maker_fee)
            elif o.side is Side.SELL and currency == base:
                held += o.leaves
        return self.balances.get(currency, 0.0) - held

    async def place_post_only(
        self, product_id: str, side: Side, base_size: float, price: float, client_order_id: str
    ) -> str:
        t0 = time.perf_counter()
        m = self.meta(product_id)
        mk = self.markets[product_id]
        size = round_step(base_size, m.base_increment)
        price = round_step(price, m.quote_increment, up=side is Side.SELL)
        bb, ba = mk.book.best_bid, mk.book.best_ask
        if bb is None or ba is None:
            raise ExchangeError("Libro no inicializado")
        if size < m.base_min_size:
            raise ExchangeError(f"Tamaño {size} < mínimo {m.base_min_size}")
        if (side is Side.BUY and price >= ba) or (side is Side.SELL and price <= bb):
            raise PostOnlyRejected("INVALID_LIMIT_PRICE_POST_ONLY")
        base = product_id.split("-")[0]
        if side is Side.BUY and self._available(self.quote) < size * price * (1 + self.maker_fee):
            raise ExchangeError("INSUFFICIENT_FUND")
        if side is Side.SELL and self._available(base) < size - 1e-12:
            raise ExchangeError("INSUFFICIENT_FUND")
        book_side = mk.book.bids if side is Side.BUY else mk.book.asks
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}",
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            price=price,
            size=size,
            queue_ahead=book_side.get(price, 0.0),  # FIFO: nos ponemos detrás
        )
        self.orders[order.order_id] = order
        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        await self._emit(self._update(order))
        return order.order_id

    async def place_market_sell(self, product_id: str, base_size: float, client_order_id: str) -> str:
        m = self.meta(product_id)
        mk = self.markets[product_id]
        base = product_id.split("-")[0]
        size = min(round_step(base_size, m.base_increment), self._available(base))
        if size <= 0:
            raise ExchangeError("INSUFFICIENT_FUND")
        bids, _ = mk.book.top(200)
        remaining, notional = size, 0.0
        for px, qty in bids:
            take = min(remaining, qty)
            notional += take * px
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > 1e-12:  # libro insuficiente: resto al peor precio - guardia
            worst = (bids[-1][0] if bids else mk.last_price) * 0.995
            notional += remaining * worst
        fee = notional * self.taker_fee
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}", client_order_id=client_order_id,
            product_id=product_id, side=Side.SELL, price=0.0, size=size,
            filled=size, notional=notional, fees=fee, status=OrderStatus.FILLED,
        )
        self.balances[base] = self.balances.get(base, 0.0) - size
        self.balances[self.quote] = self.balances.get(self.quote, 0.0) + notional - fee
        self.orders[order.order_id] = order
        await self._emit(self._update(order))
        return order.order_id

    async def place_bracket_sell(
        self, product_id: str, base_size: float, limit_price: float, stop_trigger: float, client_order_id: str
    ) -> str:
        m = self.meta(product_id)
        size = round_step(base_size, m.base_increment)
        base = product_id.split("-")[0]
        if size < m.base_min_size or self._available(base) < size - 1e-12:
            raise ExchangeError("INSUFFICIENT_FUND")
        order = _PaperOrder(
            order_id=f"paper-{uuid.uuid4().hex[:20]}", client_order_id=client_order_id, product_id=product_id,
            side=Side.SELL, price=round_step(limit_price, m.quote_increment, up=True), size=size,
            stop_trigger=round_step(stop_trigger, m.quote_increment),
            queue_ahead=self.markets[product_id].book.asks.get(limit_price, 0.0),
        )
        self.orders[order.order_id] = order
        await self._emit(self._update(order))
        return order.order_id

    async def cancel(self, order_ids: list[str]) -> None:
        for oid in order_ids:
            o = self.orders.get(oid)
            if o and o.status is OrderStatus.OPEN:
                o.status = OrderStatus.CANCELLED
                await self._emit(self._update(o))

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        o = self.orders.get(order_id)
        return self._update(o) if o else None

    async def list_open_orders(self, product_ids: list[str]) -> list[OrderUpdate]:
        return [
            self._update(o) for o in self.orders.values()
            if o.status is OrderStatus.OPEN and o.product_id in product_ids
        ]

    async def ping(self) -> float:
        await self._call(self.public.get_unix_time)
        return self.last_latency_ms

    async def fee_rates(self) -> tuple[float, float] | None:
        return self.maker_fee, self.taker_fee

    def _update(self, o: _PaperOrder) -> OrderUpdate:
        return OrderUpdate(
            order_id=o.order_id, client_order_id=o.client_order_id, product_id=o.product_id,
            side=o.side, status=o.status, filled_size=o.filled, avg_price=o.avg,
            total_fees=o.fees, leaves_size=o.leaves, limit_price=o.price,
        )

    async def on_market_trade(self, product_id: str, price: float, size: float) -> None:
        """Matching de órdenes límite contra el flujo de trades real."""
        for o in list(self.orders.values()):
            if o.status is not OrderStatus.OPEN or o.product_id != product_id:
                continue
            if o.stop_trigger > 0 and price <= o.stop_trigger:
                # disparo del stop del bracket: ejecución a mercado (taker) al precio negociado
                fill = o.leaves
                notional = fill * price
                fee = notional * self.taker_fee
                base = product_id.split("-")[0]
                o.filled += fill
                o.notional += notional
                o.fees += fee
                self.balances[base] = self.balances.get(base, 0.0) - fill
                self.balances[self.quote] = self.balances.get(self.quote, 0.0) + notional - fee
                o.status = OrderStatus.FILLED
                await self._emit(self._update(o))
                continue
            crossed = (o.side is Side.BUY and price < o.price) or (o.side is Side.SELL and price > o.price)
            at_level = math.isclose(price, o.price, rel_tol=0, abs_tol=1e-12)
            fill = 0.0
            if crossed:
                fill = o.leaves                      # el precio atravesó nuestro nivel
            elif at_level:
                consume = size
                if o.queue_ahead > 0:
                    eat = min(o.queue_ahead, consume)
                    o.queue_ahead -= eat
                    consume -= eat
                fill = min(o.leaves, consume)
            if fill <= 0:
                continue
            notional = fill * o.price
            fee = notional * self.maker_fee
            base = product_id.split("-")[0]
            o.filled += fill
            o.notional += notional
            o.fees += fee
            if o.side is Side.BUY:
                self.balances[self.quote] = self.balances.get(self.quote, 0.0) - notional - fee
                self.balances[base] = self.balances.get(base, 0.0) + fill
            else:
                self.balances[base] = self.balances.get(base, 0.0) - fill
                self.balances[self.quote] = self.balances.get(self.quote, 0.0) + notional - fee
            if o.leaves <= 1e-12:
                o.status = OrderStatus.FILLED
            await self._emit(self._update(o))
