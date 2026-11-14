"""Plataforma web: sesión, estado, comandos, credenciales cifradas y portafolio."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3creto-largo")
    monkeypatch.setenv("CREDENTIALS_KEY", "clave-de-cifrado-de-prueba-123")
    import config
    importlib.reload(config)
    import credentials
    importlib.reload(credentials)
    import dashboard
    importlib.reload(dashboard)
    return dashboard


def _login(c):
    r = c.post("/api/login", json={"password": "s3creto-largo"})
    assert r.status_code == 200


def test_auth_required_and_login(web):
    c = TestClient(web.app)
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/login", json={"password": "mal"}).status_code == 401
    _login(c)
    r = c.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert [p["product_id"] for p in body["pairs"]] == ["SOL-USDC", "BTC-USDC", "ETH-USDC", "SUI-USDC"]
    assert c.get("/healthz").status_code == 200


def test_commands_need_confirmation(web):
    c = TestClient(web.app)
    _login(c)
    assert c.post("/api/command", json={"command": "PANIC"}).status_code == 400
    assert c.post("/api/command", json={"command": "PANIC", "confirm": "PANIC"}).status_code == 200
    assert c.post("/api/command", json={"command": "SET_MODE", "payload": {"mode": "LIVE"}}).status_code == 400
    assert c.post("/api/command", json={"command": "DROP"}).status_code == 400
    assert web.db.pending_commands()[0]["command"] == "PANIC"


def test_credentials_encrypted_roundtrip_and_portfolio(web, monkeypatch):
    perms = {"can_view": True, "can_trade": True, "can_transfer": False,
             "portfolio_uuid": "u1", "portfolio_type": "DEFAULT"}
    monkeypatch.setattr(web, "validate_credentials", lambda k, s: perms)
    monkeypatch.setattr(web, "detect_portfolio", lambda k, s: {
        "portfolio_name": "Default", "total_usd": 1234.5, "cash_usd": 1000.0,
        "positions": [{"asset": "USDC", "value_usd": 1000.0}], "permissions": perms, "fetched_at": time.time()})
    c = TestClient(web.app)
    _login(c)
    key = "organizations/abc/apiKeys/def-123-456"
    secret = "-----BEGIN EC PRIVATE KEY-----\\nMHcCAQEEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\\n-----END EC PRIVATE KEY-----"
    r = c.post("/api/credentials", json={"api_key": key, "api_secret": secret})
    assert r.status_code == 200, r.text
    assert r.json()["portfolio"]["total_usd"] == 1234.5
    raw = web.db.get_kv("coinbase_credentials_enc")
    assert key not in raw and "PRIVATE" not in raw            # cifrado en reposo
    st = c.get("/api/credentials").json()
    assert st["configured"] and key not in st["masked_key"]
    assert any(cmd["command"] == "RELOAD_CREDENTIALS" for cmd in web.db.pending_commands())
    from credentials import load_credentials
    loaded = load_credentials(web.db)
    assert loaded.api_key == key and loaded.api_secret.startswith("-----BEGIN EC PRIVATE KEY-----\n")
    assert c.get("/api/portfolio").json()["configured"] is True
    assert c.delete("/api/credentials").status_code == 200
    assert c.get("/api/credentials").json()["configured"] is False


def test_transfer_permission_rejected(monkeypatch):
    import credentials

    class FakeClient:
        def get_api_key_permissions(self):
            return {"can_view": True, "can_trade": True, "can_transfer": True}

    monkeypatch.setattr(credentials, "_client", lambda k, s: FakeClient())
    with pytest.raises(credentials.CredentialError, match="TRANSFERENCIA"):
        credentials.validate_credentials("organizations/a/apiKeys/b",
                                         "-----BEGIN EC PRIVATE KEY-----\nX\n-----END EC PRIVATE KEY-----")
