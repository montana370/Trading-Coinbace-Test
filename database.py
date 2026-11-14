"""
database.py — Estado compartido entre procesos (SQLite en modo WAL).

* El Core Engine es el ÚNICO escritor de estado de mercado/posiciones.
* El Dashboard (y cualquier otro proceso) lee concurrentemente gracias a WAL
  y sólo escribe en la tabla `commands` (bus de órdenes de control).
* En el engine, todas las llamadas pasan por `AsyncDatabase`, que serializa
  el I/O en un único hilo dedicado → el event loop nunca se bloquea por disco.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pair_snapshot (
    product_id          TEXT PRIMARY KEY,
    role                TEXT,
    price               REAL,
    best_bid            REAL,
    best_ask            REAL,
    spread_pct          REAL,
    max_spread_pct      REAL,
    obi                 REAL,
    atr                 REAL,
    atr_pct             REAL,
    regime              TEXT,
    hurst               REAL,
    adx                 REAL,
    vwap                REAL,
    cvd_5m              REAL,
    projected_move_pct  REAL,
    fee_filter_ok       INTEGER,
    fee_filter_reason   TEXT,
    signal              TEXT,
    updated_at          REAL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id      TEXT PRIMARY KEY,
    product_id       TEXT NOT NULL,
    strategy         TEXT,
    regime           TEXT,
    entry_price      REAL,
    size             REAL,
    initial_size     REAL,
    stop_price       REAL,
    initial_stop     REAL,
    take_profit      REAL,
    breakeven_price  REAL,
    breakeven_armed  INTEGER,
    mark_price       REAL,
    unrealized_net   REAL,
    entry_fee_paid   REAL,
    opened_at        REAL,
    status           TEXT NOT NULL,      -- OPEN | CLOSED
    mode             TEXT,
    state_json       TEXT,
    updated_at       REAL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id         TEXT PRIMARY KEY,
    client_order_id  TEXT,
    product_id       TEXT,
    side             TEXT,
    purpose          TEXT,               -- ENTRY | TAKE_PROFIT | EXIT
    price            REAL,
    size             REAL,
    filled_size      REAL,
    status           TEXT,
    mode             TEXT,
    created_at       REAL,
    updated_at       REAL
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id      TEXT PRIMARY KEY,
    position_id   TEXT,
    product_id    TEXT,
    strategy      TEXT,
    entry_price   REAL,
    exit_price    REAL,
    size          REAL,
    gross_pnl     REAL,
    fees          REAL,
    net_pnl       REAL,
    net_pct       REAL,
    opened_at     REAL,
    closed_at     REAL,
    duration_s    REAL,
    exit_reason   TEXT,
    mode          TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts          REAL NOT NULL,
    equity      REAL NOT NULL,
    cash        REAL,
    unrealized  REAL,
    mode        TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command       TEXT NOT NULL,
    payload       TEXT,
    source        TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    processed_at  REAL,
    result        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_pending ON commands(processed_at, priority);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    level     TEXT,
    category  TEXT,
    message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

COMMAND_PRIORITY = {"PANIC": 100, "PAUSE": 50, "RELOAD_CREDENTIALS": 30, "SET_MODE": 20, "RESUME": 10}


class Database:
    """Acceso síncrono, thread-safe. Usado por el Dashboard y por AsyncDatabase."""

    def __init__(self, path: Path | str, readonly: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=5.0, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA temp_store=MEMORY;")
        if not readonly:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Migraciones aditivas e idempotentes."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col in ("initial_stop", "take_profit"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_product ON trades(product_id, closed_at)")

    # ── utilidades básicas ──────────────────────────────────────────────────
    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, [tuple(r) for r in rows])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── KV de estado global ─────────────────────────────────────────────────
    def set_kv(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    def set_many_kv(self, items: dict[str, Any]) -> None:
        now = time.time()
        self.executemany(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(k, json.dumps(v), now) for k, v in items.items()],
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def all_kv(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key, value FROM kv")}

    # ── Snapshots de mercado ────────────────────────────────────────────────
    def upsert_pair_snapshots(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "product_id")
        sql = (
            f"INSERT INTO pair_snapshot({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(product_id) DO UPDATE SET {updates}"
        )
        self.executemany(sql, [[r[c] for c in cols] for r in rows])

    # ── Posiciones / órdenes / trades ──────────────────────────────────────
    def upsert_position(self, row: dict[str, Any]) -> None:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "position_id")
        self.execute(
            f"INSERT INTO positions({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(position_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )

    def upsert_positions(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.upsert_position(r)

    def open_positions(self, mode: str | None = None) -> list[dict[str, Any]]:
        if mode:
            return self.query(
                "SELECT * FROM positions WHERE status='OPEN' AND mode=? ORDER BY opened_at", (mode,)
            )
        return self.query("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at")

    def upsert_order(self, row: dict[str, Any]) -> None:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "order_id")
        self.execute(
            f"INSERT INTO orders({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(order_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )

    def active_orders(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM orders WHERE status IN ('PENDING','OPEN') ORDER BY created_at DESC"
        )

    def insert_trade(self, row: dict[str, Any]) -> None:
        cols = list(row.keys())
        self.execute(
            f"INSERT OR REPLACE INTO trades({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )

    def trades_for(self, product_id: str, mode: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM trades WHERE product_id=? AND mode=? ORDER BY closed_at DESC LIMIT ?",
            (product_id, mode, limit),
        )

    def trades(self, mode: str | None = None, since: float = 0.0) -> list[dict[str, Any]]:
        if mode:
            return self.query(
                "SELECT * FROM trades WHERE mode=? AND closed_at>=? ORDER BY closed_at", (mode, since)
            )
        return self.query("SELECT * FROM trades WHERE closed_at>=? ORDER BY closed_at", (since,))

    def insert_equity(self, ts: float, equity: float, cash: float, unrealized: float, mode: str) -> None:
        self.execute(
            "INSERT INTO equity(ts, equity, cash, unrealized, mode) VALUES(?,?,?,?,?)",
            (ts, equity, cash, unrealized, mode),
        )

    def equity_curve(self, mode: str, since: float = 0.0, max_points: int = 5000) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT ts, equity, cash, unrealized FROM equity WHERE mode=? AND ts>=? ORDER BY ts",
            (mode, since),
        )
        if len(rows) > max_points:
            step = len(rows) // max_points + 1
            rows = rows[::step] + [rows[-1]]
        return rows

    # ── Bus de comandos ─────────────────────────────────────────────────────
    def push_command(self, command: str, source: str, payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO commands(command, payload, source, priority, created_at) VALUES(?,?,?,?,?)",
                (
                    command,
                    json.dumps(payload or {}),
                    source,
                    COMMAND_PRIORITY.get(command, 0),
                    time.time(),
                ),
            )
            return int(cur.lastrowid or 0)

    def pending_commands(self) -> list[dict[str, Any]]:
        """Comandos no procesados; PANIC siempre primero (prioridad máxima)."""
        rows = self.query(
            "SELECT * FROM commands WHERE processed_at IS NULL ORDER BY priority DESC, id ASC"
        )
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return rows

    def ack_command(self, command_id: int, result: str) -> None:
        self.execute(
            "UPDATE commands SET processed_at=?, result=? WHERE id=?", (time.time(), result, command_id)
        )

    def recent_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))

    # ── Eventos ─────────────────────────────────────────────────────────────
    def log_event(self, level: str, category: str, message: str) -> None:
        self.execute(
            "INSERT INTO events(ts, level, category, message) VALUES(?,?,?,?)",
            (time.time(), level, category, message),
        )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def prune(self, keep_days: float = 30.0) -> None:
        cutoff = time.time() - keep_days * 86400
        self.execute("DELETE FROM events WHERE ts<?", (cutoff,))
        self.execute("DELETE FROM equity WHERE ts<?", (cutoff,))
        self.execute("DELETE FROM commands WHERE processed_at IS NOT NULL AND processed_at<?", (cutoff,))


class AsyncDatabase:
    """Fachada asíncrona: todo el I/O se ejecuta en un único hilo dedicado."""

    def __init__(self, path: Path | str) -> None:
        self.sync = Database(path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def fire(self, fn: Callable[..., Any], *args: Any) -> None:
        """Escritura 'fire-and-forget' (logs/telemetría) sin esperar."""
        self._executor.submit(fn, *args)

    async def close(self) -> None:
        await self.run(self.sync.close)
        self._executor.shutdown(wait=True)
