"""
credentials.py — Almacén cifrado de claves API + detección de portafolio.

* Las claves CDP se guardan cifradas (Fernet / AES-128-CBC + HMAC-SHA256) en
  la tabla `kv` de SQLite. La clave de cifrado se deriva de `CREDENTIALS_KEY`
  (variable de entorno, NUNCA en la base de datos).
* Antes de guardar, se validan contra Coinbase (`get_api_key_permissions`) y
  se rechazan claves con permiso de TRANSFERENCIA (principio de mínimo
  privilegio: el bot sólo necesita ver y operar).
* `detect_portfolio` devuelve el desglose del portafolio de la clave.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from database import Database

KV_KEY = "coinbase_credentials_enc"
STABLES = {"USD", "USDC", "USDT", "DAI", "PYUSD", "EURC"}


class CredentialError(Exception):
    pass


@dataclass
class StoredCredentials:
    api_key: str
    api_secret: str
    saved_at: float
    permissions: dict[str, Any]

    @property
    def masked_key(self) -> str:
        k = self.api_key
        return f"{k[:18]}…{k[-6:]}" if len(k) > 28 else "…" + k[-4:]


def _fernet() -> Fernet:
    secret = os.getenv("CREDENTIALS_KEY", "")
    if len(secret) < 16:
        raise CredentialError("CREDENTIALS_KEY no configurada (mín. 16 caracteres) en el servidor")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def normalize_secret(secret: str) -> str:
    """Acepta la clave PEM con saltos reales o con '\\n' literales."""
    s = secret.strip().replace("\\n", "\n").replace("\r\n", "\n")
    return s + "\n"


def save_credentials(db: Database, api_key: str, api_secret: str, permissions: dict[str, Any]) -> StoredCredentials:
    creds = StoredCredentials(api_key.strip(), normalize_secret(api_secret), time.time(), permissions)
    token = _fernet().encrypt(json.dumps({
        "api_key": creds.api_key, "api_secret": creds.api_secret,
        "saved_at": creds.saved_at, "permissions": permissions,
    }).encode())
    db.set_kv(KV_KEY, token.decode())
    return creds


def load_credentials(db: Database) -> StoredCredentials | None:
    token = db.get_kv(KV_KEY)
    if not token:
        return None
    try:
        data = json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, CredentialError):
        return None
    return StoredCredentials(data["api_key"], data["api_secret"], data["saved_at"], data.get("permissions", {}))


def delete_credentials(db: Database) -> None:
    db.execute("DELETE FROM kv WHERE key=?", (KV_KEY,))


def _to_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    return resp.to_dict() if hasattr(resp, "to_dict") else dict(getattr(resp, "__dict__", {}))


def _client(api_key: str, api_secret: str) -> Any:
    from coinbase.rest import RESTClient

    return RESTClient(api_key=api_key, api_secret=api_secret, timeout=10)


def validate_credentials(api_key: str, api_secret: str) -> dict[str, Any]:
    """Valida contra Coinbase. Lanza CredentialError con un mensaje legible."""
    api_key, api_secret = api_key.strip(), normalize_secret(api_secret)
    if not api_key.startswith("organizations/") or "/apiKeys/" not in api_key:
        raise CredentialError("El API Key debe tener el formato organizations/{org_id}/apiKeys/{key_id}")
    if "BEGIN EC PRIVATE KEY" not in api_secret:
        raise CredentialError("El secreto debe ser la clave privada PEM (-----BEGIN EC PRIVATE KEY-----). "
                              "Crea la clave con el algoritmo ECDSA, no Ed25519.")
    try:
        perms = _to_dict(_client(api_key, api_secret).get_api_key_permissions())
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg:
            raise CredentialError("Coinbase rechazó la clave (401). Revisa que el API Key y el secreto "
                                  "correspondan a la misma clave y que no esté revocada.") from exc
        raise CredentialError(f"No se pudo validar con Coinbase: {msg[:200]}") from exc
    if not perms.get("can_view"):
        raise CredentialError("La clave no tiene permiso de lectura (View).")
    if perms.get("can_transfer"):
        raise CredentialError("Por seguridad no se aceptan claves con permiso de TRANSFERENCIA. "
                              "Crea una clave sólo con View + Trade.")
    return perms


def detect_portfolio(api_key: str, api_secret: str) -> dict[str, Any]:
    """Desglose del portafolio asociado a la clave (saldo, asignación y PnL por activo)."""
    client = _client(api_key, api_secret)
    perms = _to_dict(client.get_api_key_permissions())
    portfolio_uuid = perms.get("portfolio_uuid")
    name = None
    portfolios = _to_dict(client.get_portfolios()).get("portfolios", [])
    for p in portfolios:
        p = _to_dict(p)
        if p.get("uuid") == portfolio_uuid:
            name = p.get("name")
    positions: list[dict[str, Any]] = []
    total = 0.0
    if portfolio_uuid:
        bd = _to_dict(_to_dict(client.get_portfolio_breakdown(portfolio_uuid=portfolio_uuid)).get("breakdown"))
        for sp in bd.get("spot_positions", []) or []:
            sp = _to_dict(sp)
            fiat = float(sp.get("total_balance_fiat") or 0.0)
            crypto = float(sp.get("total_balance_crypto") or 0.0)
            if fiat < 0.01 and crypto <= 0:
                continue
            positions.append({
                "asset": sp.get("asset"),
                "balance": crypto,
                "value_usd": fiat,
                "available_usd": float(sp.get("available_to_trade_fiat") or 0.0),
                "allocation": float(sp.get("allocation") or 0.0),
                "cost_basis": float(_to_dict(sp.get("cost_basis")).get("value") or 0.0),
                "unrealized_pnl": float(sp.get("unrealized_pnl") or 0.0),
                "is_cash": bool(sp.get("is_cash")) or sp.get("asset") in STABLES,
            })
            total += fiat
    positions.sort(key=lambda r: r["value_usd"], reverse=True)
    return {
        "portfolio_uuid": portfolio_uuid,
        "portfolio_name": name or perms.get("portfolio_type") or "Default",
        "portfolio_type": perms.get("portfolio_type"),
        "permissions": {"can_view": perms.get("can_view"), "can_trade": perms.get("can_trade"),
                        "can_transfer": perms.get("can_transfer")},
        "total_usd": total,
        "cash_usd": sum(p["value_usd"] for p in positions if p["is_cash"]),
        "positions": positions,
        "fetched_at": time.time(),
    }
