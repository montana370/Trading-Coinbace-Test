"""
dashboard.py — Plataforma web (FastAPI + SPA móvil) — proceso independiente del Core Engine.

* Lee el estado compartido en SQLite WAL y escribe sólo en el bus `commands`
  (y en el almacén cifrado de credenciales).
* Sesión por cookie firmada (HMAC-SHA256), HttpOnly + SameSite=Strict.
* Gráficas: velas oficiales de Coinbase (endpoint público, en caché) dibujadas
  en el navegador con TradingView Lightweight Charts, con entradas, salidas,
  SL/TP y breakeven marcados.
* Sesión de configuración de API: valida la clave contra Coinbase, la guarda
  cifrada, detecta el portafolio y ordena al motor recargarla.

Uso:  python dashboard.py      (escucha en $PORT, por defecto 8501)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import SETTINGS
from credentials import (
    CredentialError,
    delete_credentials,
    detect_portfolio,
    load_credentials,
    save_credentials,
    validate_credentials,
)
from database import Database

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
COOKIE = "qt_session"
SESSION_TTL = 7 * 86400
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
COINBASE_PUBLIC = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"

TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1m": ("ONE_MINUTE", 60),
    "5m": ("FIVE_MINUTE", 300),
    "15m": ("FIFTEEN_MINUTE", 900),
    "1h": ("ONE_HOUR", 3600),
    "6h": ("SIX_HOUR", 21600),
    "1d": ("ONE_DAY", 86400),
}

db = Database(SETTINGS.runtime.db_path)
app = FastAPI(title="Quant Terminal", docs_url=None, redoc_url=None, openapi_url=None)


# ═════════════════════════════════════════════════════════════════════════════
# Sesión
# ═════════════════════════════════════════════════════════════════════════════
def _session_secret() -> bytes:
    secret = db.get_kv("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_kv("session_secret", secret)
    return hashlib.sha256((secret + PASSWORD).encode()).digest()


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest()).decode()


def make_token() -> str:
    payload = f"{int(time.time()) + SESSION_TTL}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def token_valid(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(f"{exp}.{nonce}")):
        return False
    return exp.isdigit() and int(exp) > time.time()


def require_auth(request: Request) -> None:
    if PASSWORD and not token_valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="No autenticado")


_failed_logins: dict[str, list[float]] = {}


class LoginBody(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0]
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < 900]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera 15 minutos.")
    if not PASSWORD or hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        _failed_logins.pop(ip, None)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, make_token(), max_age=SESSION_TTL, httponly=True,
                            secure=secure, samesite="strict")
        return {"ok": True}
    recent.append(now)
    _failed_logins[ip] = recent
    await asyncio.sleep(1.0)
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    return {"authenticated": (not PASSWORD) or token_valid(request.cookies.get(COOKIE)),
            "auth_required": bool(PASSWORD)}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    hb = db.get_kv("engine_heartbeat", 0) or 0
    return {"ok": True, "engine_alive": time.time() - hb < 10}


# ═════════════════════════════════════════════════════════════════════════════
# Estado / métricas
# ═════════════════════════════════════════════════════════════════════════════
def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def compute_kpis(trades: list[dict[str, Any]], equity: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"trades": len(trades), "gross": 0.0, "net": 0.0, "fees": 0.0,
                           "win_rate": None, "profit_factor": None, "avg_net": None,
                           "drawdown_pct": 0.0, "max_drawdown_pct": 0.0, "best": None, "worst": None}
    if trades:
        nets = [t["net_pnl"] for t in trades]
        wins = [n for n in nets if n > 0]
        losses = [-n for n in nets if n < 0]
        out.update(
            gross=sum(t["gross_pnl"] for t in trades), net=sum(nets), fees=sum(t["fees"] for t in trades),
            win_rate=len(wins) / len(trades) * 100, avg_net=sum(nets) / len(nets),
            profit_factor=(sum(wins) / sum(losses)) if losses else None,
            best=max(nets), worst=min(nets),
        )
    if equity:
        peak, max_dd = 0.0, 0.0
        for row in equity:
            peak = max(peak, row["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - row["equity"]) / peak * 100)
        last = equity[-1]["equity"]
        out["drawdown_pct"] = (peak - last) / peak * 100 if peak > 0 else 0.0
        out["max_drawdown_pct"] = max_dd
    return out


def _position_view(p: dict[str, Any]) -> dict[str, Any]:
    st = json.loads(p.get("state_json") or "{}")
    mark = p.get("mark_price") or p["entry_price"]
    return {
        "position_id": p["position_id"], "product_id": p["product_id"], "strategy": p["strategy"],
        "regime": p["regime"], "entry_price": p["entry_price"], "size": p["size"],
        "initial_size": p["initial_size"], "stop_price": p["stop_price"], "initial_stop": p["initial_stop"],
        "take_profit": p["take_profit"], "breakeven_price": p["breakeven_price"],
        "breakeven_armed": bool(p["breakeven_armed"]), "mark_price": mark,
        "unrealized_net": p["unrealized_net"], "entry_fee_paid": p["entry_fee_paid"],
        "opened_at": p["opened_at"], "notional": mark * p["size"],
        "dist_sl_pct": (mark - p["stop_price"]) / mark * 100 if mark else None,
        "dist_tp_pct": (p["take_profit"] - mark) / mark * 100 if mark else None,
        "tp_levels": [{"price": lv["price"], "fraction": lv["fraction"], "filled": lv.get("filled", False)}
                      for lv in st.get("tp_levels", [])],
    }


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state() -> JSONResponse:
    kv = db.all_kv()
    mode = kv.get("mode", "PAPER")
    hb = kv.get("engine_heartbeat") or 0
    pairs = {r["product_id"]: {k: _clean(v) for k, v in r.items()} for r in db.query("SELECT * FROM pair_snapshot")}
    for pid, cfg in SETTINGS.pairs.items():
        pairs.setdefault(pid, {"product_id": pid})
        pairs[pid].update(role=cfg.role.value, max_spread_pct=cfg.max_spread_pct,
                          max_allocation=cfg.max_allocation, base=cfg.base_currency)
    trades = db.trades(mode)
    since = time.time() - 30 * 86400
    equity = db.equity_curve(mode, since, max_points=600)
    day_start = time.time() - (time.time() % 86400)
    today = [t for t in trades if t["closed_at"] >= day_start]
    body = {
        "server_time": time.time(),
        "engine_alive": time.time() - hb < 10,
        "telemetry": {k: kv.get(k) for k in (
            "bot_status", "status_detail", "mode", "ws_latency_ms", "api_latency_ms", "heartbeat_age_s",
            "ws_connected", "oracle", "equity", "cash", "day_start_equity", "daily_dd_pct", "fees",
            "consecutive_losses", "open_positions", "max_open_positions", "credentials_source", "live_allowed",
        )},
        "pairs": [pairs[p] for p in SETTINGS.pairs],
        "positions": [_position_view(p) for p in db.open_positions(mode)],
        "orders": [o for o in db.active_orders() if o["mode"] == mode],
        "kpis": compute_kpis(trades, equity),
        "kpis_today": compute_kpis(today),
        "equity": [{"t": r["ts"], "v": r["equity"]} for r in equity],
    }
    return JSONResponse(body)


@app.get("/api/asset/{product_id}", dependencies=[Depends(require_auth)])
async def asset(product_id: str) -> dict[str, Any]:
    if product_id not in SETTINGS.pairs:
        raise HTTPException(404, "Activo desconocido")
    mode = db.get_kv("mode", "PAPER")
    trades = db.trades_for(product_id, mode)
    pos = [_position_view(p) for p in db.open_positions(mode) if p["product_id"] == product_id]
    return {"product_id": product_id, "trades": trades, "kpis": compute_kpis(trades),
            "position": pos[0] if pos else None,
            "orders": [o for o in db.active_orders() if o["mode"] == mode and o["product_id"] == product_id]}


# ═════════════════════════════════════════════════════════════════════════════
# Velas (Coinbase pública, en caché)
# ═════════════════════════════════════════════════════════════════════════════
_candle_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_candle_locks: dict[tuple[str, str], asyncio.Lock] = {}
_http: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "quant-terminal/1.0"})
    return _http


@app.get("/api/candles/{product_id}", dependencies=[Depends(require_auth)])
async def candles(product_id: str, tf: str = "5m") -> dict[str, Any]:
    if product_id not in SETTINGS.pairs or tf not in TIMEFRAMES:
        raise HTTPException(400, "Parámetros inválidos")
    key = (product_id, tf)
    gran, secs = TIMEFRAMES[tf]
    ttl = 4.0 if secs <= 300 else 15.0
    lock = _candle_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _candle_cache.get(key)
        if cached and time.time() - cached[0] < ttl:
            return {"tf": tf, "seconds": secs, "candles": cached[1]}
        end = int(time.time())
        start = end - secs * 300
        try:
            client = await _client()
            r = await client.get(COINBASE_PUBLIC.format(pid=product_id),
                                 params={"start": start, "end": end, "granularity": gran})
            r.raise_for_status()
            rows = r.json().get("candles", [])
        except Exception as exc:  # noqa: BLE001
            if cached:
                return {"tf": tf, "seconds": secs, "candles": cached[1], "stale": True}
            raise HTTPException(502, f"Coinbase no disponible: {exc}") from exc
        data = sorted(
            ({"time": int(c["start"]), "open": float(c["open"]), "high": float(c["high"]),
              "low": float(c["low"]), "close": float(c["close"]), "volume": float(c["volume"])} for c in rows),
            key=lambda c: c["time"],
        )
        _candle_cache[key] = (time.time(), data)
        return {"tf": tf, "seconds": secs, "candles": data}


# ═════════════════════════════════════════════════════════════════════════════
# Comandos
# ═════════════════════════════════════════════════════════════════════════════
class CommandBody(BaseModel):
    command: str
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm: str = ""


@app.post("/api/command", dependencies=[Depends(require_auth)])
async def command(body: CommandBody) -> dict[str, Any]:
    cmd = body.command.upper()
    if cmd not in {"PANIC", "PAUSE", "RESUME", "SET_MODE"}:
        raise HTTPException(400, "Comando no permitido")
    if cmd == "PANIC" and body.confirm != "PANIC":
        raise HTTPException(400, "Confirmación requerida")
    if cmd == "SET_MODE":
        mode = str(body.payload.get("mode", "")).upper()
        if mode not in {"PAPER", "LIVE"}:
            raise HTTPException(400, "Modo inválido")
        if mode == "LIVE" and body.confirm != "LIVE":
            raise HTTPException(400, "Escribe LIVE para confirmar")
        body.payload = {"mode": mode}
    cid = db.push_command(cmd, "web", body.payload)
    return {"ok": True, "id": cid}


@app.get("/api/commands/{cid}", dependencies=[Depends(require_auth)])
async def command_status(cid: int) -> dict[str, Any]:
    rows = db.query("SELECT id, command, result, processed_at FROM commands WHERE id=?", (cid,))
    if not rows:
        raise HTTPException(404, "No existe")
    return rows[0]


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def events(limit: int = 60) -> list[dict[str, Any]]:
    return db.recent_events(min(limit, 200))


# ═════════════════════════════════════════════════════════════════════════════
# Credenciales y portafolio
# ═════════════════════════════════════════════════════════════════════════════
class CredentialsBody(BaseModel):
    api_key: str = Field(min_length=10, max_length=300)
    api_secret: str = Field(min_length=50, max_length=5000)


_portfolio_cache: dict[str, Any] = {}


def _encryption_ready() -> bool:
    return len(os.getenv("CREDENTIALS_KEY", "")) >= 16


@app.get("/api/credentials", dependencies=[Depends(require_auth)])
async def get_credentials() -> dict[str, Any]:
    stored = load_credentials(db) if _encryption_ready() else None
    return {
        "encryption_ready": _encryption_ready(),
        "configured": stored is not None,
        "masked_key": stored.masked_key if stored else None,
        "saved_at": stored.saved_at if stored else None,
        "permissions": stored.permissions if stored else None,
        "engine_source": db.get_kv("credentials_source", "ninguna"),
        "live_allowed": bool(db.get_kv("live_allowed", False)),
    }


@app.post("/api/credentials", dependencies=[Depends(require_auth)])
async def post_credentials(body: CredentialsBody) -> dict[str, Any]:
    if not _encryption_ready():
        raise HTTPException(503, "El servidor no tiene CREDENTIALS_KEY configurada")
    try:
        perms = await asyncio.to_thread(validate_credentials, body.api_key, body.api_secret)
        stored = save_credentials(db, body.api_key, body.api_secret, perms)
    except CredentialError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.push_command("RELOAD_CREDENTIALS", "web")
    _portfolio_cache.clear()
    try:
        portfolio = await asyncio.to_thread(detect_portfolio, stored.api_key, stored.api_secret)
        _portfolio_cache.update(data=portfolio, ts=time.time())
    except Exception as exc:  # noqa: BLE001
        portfolio = {"error": f"Clave válida, pero no se pudo leer el portafolio: {str(exc)[:160]}"}
    return {"ok": True, "masked_key": stored.masked_key, "permissions": perms, "portfolio": portfolio}


@app.delete("/api/credentials", dependencies=[Depends(require_auth)])
async def remove_credentials() -> dict[str, Any]:
    delete_credentials(db)
    _portfolio_cache.clear()
    db.push_command("RELOAD_CREDENTIALS", "web")
    return {"ok": True}


@app.get("/api/portfolio", dependencies=[Depends(require_auth)])
async def portfolio(refresh: bool = False) -> dict[str, Any]:
    stored = load_credentials(db) if _encryption_ready() else None
    if stored is None:
        return {"configured": False}
    if not refresh and _portfolio_cache.get("data") and time.time() - _portfolio_cache.get("ts", 0) < 30:
        return {"configured": True, **_portfolio_cache["data"]}
    try:
        data = await asyncio.to_thread(detect_portfolio, stored.api_key, stored.api_secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo leer el portafolio: {str(exc)[:200]}") from exc
    _portfolio_cache.update(data=data, ts=time.time())
    return {"configured": True, **data}


# ═════════════════════════════════════════════════════════════════════════════
# SPA
# ═════════════════════════════════════════════════════════════════════════════
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8501")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
