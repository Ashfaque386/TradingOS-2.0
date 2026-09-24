"""Broker OAuth callback URL: visible before any credential exists, and
operator-editable (Phase 17 local Docker pass).

Zerodha and Upstox only issue an API key *after* a redirect URL is
registered in their developer console, but the Settings page only showed
the URL after "Connect", which itself needs a saved key. The status
endpoint now always reports the effective URL. A custom URL can be saved
with the keys, or on its own later via PUT .../redirect-uri, and Connect
uses it. Also here: the post-login landing goes to FRONTEND_BASE_URL (the
frontend's origin), not this backend's own /settings, which 404s in the
multi-container stack.
"""

from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.brokers.base import BrokerCredentials
from src.core.config import get_settings
from src.core.roles import Role
from src.main import app
from src.security.secrets_store import SecretsStore

CUSTOM_ZERODHA = "https://tunnel.example.com/api/v1/broker-credentials/zerodha/callback"
CUSTOM_UPSTOX = "https://tunnel.example.com/api/v1/broker-credentials/upstox/callback"


def _override_store(tmp_path) -> SecretsStore:
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_broker_credentials_store] = lambda: store
    return store


def _clear_overrides():
    app.dependency_overrides.pop(get_broker_credentials_store, None)


async def _admin(client: AsyncClient, make_user, role=Role.SYSTEM_ADMINISTRATOR) -> dict:
    await make_user("u@example.com", "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _status(client: AsyncClient, headers: dict) -> dict:
    resp = await client.get("/api/v1/broker-credentials", headers=headers)
    assert resp.status_code == 200
    return {row["broker"]: row for row in resp.json()}


async def test_default_redirect_shown_before_any_credentials(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        headers = await _admin(client, make_user)
        by_broker = await _status(client, headers)
        for broker in ("zerodha", "upstox"):
            row = by_broker[broker]
            assert row["configured"] is False
            expected_suffix = f"/api/v1/broker-credentials/{broker}/callback"
            assert row["redirect_uri"].endswith(expected_suffix)
            assert row["redirect_uri"] == row["default_redirect_uri"]
            assert row["redirect_uri_is_custom"] is False
    finally:
        _clear_overrides()


async def test_custom_redirect_saved_with_keys_is_used_by_connect(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        headers = await _admin(client, make_user)
        resp = await client.post(
            "/api/v1/broker-credentials/upstox",
            json={"api_key": "K", "api_secret": "S", "redirect_uri": CUSTOM_UPSTOX},
            headers=headers,
        )
        assert resp.status_code == 204

        row = (await _status(client, headers))["upstox"]
        assert row["redirect_uri"] == CUSTOM_UPSTOX
        assert row["redirect_uri_is_custom"] is True

        resp = await client.get("/api/v1/broker-credentials/upstox/login-url", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["redirect_uri"] == CUSTOM_UPSTOX
        sent = parse_qs(urlsplit(body["login_url"]).query)["redirect_uri"]
        assert sent == [CUSTOM_UPSTOX]
    finally:
        _clear_overrides()


async def test_resaving_keys_without_redirect_keeps_the_custom_one(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials(
        "zerodha", BrokerCredentials(api_key="old", redirect_uri_override=CUSTOM_ZERODHA)
    )
    try:
        headers = await _admin(client, make_user)
        resp = await client.post(
            "/api/v1/broker-credentials/zerodha",
            json={"api_key": "new", "api_secret": "S"},
            headers=headers,
        )
        assert resp.status_code == 204
        assert store.get_credentials("zerodha").redirect_uri_override == CUSTOM_ZERODHA
        assert store.get_credentials("zerodha").api_key == "new"
    finally:
        _clear_overrides()


async def test_put_redirect_uri_sets_and_resets_without_touching_keys(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials(
        "zerodha", BrokerCredentials(api_key="K", api_secret="S", access_token="T")
    )
    try:
        headers = await _admin(client, make_user)
        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": CUSTOM_ZERODHA},
            headers=headers,
        )
        assert resp.status_code == 204
        creds = store.get_credentials("zerodha")
        assert creds.redirect_uri_override == CUSTOM_ZERODHA
        assert (creds.api_key, creds.api_secret, creds.access_token) == ("K", "S", "T")

        resp = await client.get("/api/v1/broker-credentials/zerodha/login-url", headers=headers)
        assert resp.json()["redirect_uri"] == CUSTOM_ZERODHA

        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": None},
            headers=headers,
        )
        assert resp.status_code == 204
        row = (await _status(client, headers))["zerodha"]
        assert row["redirect_uri_is_custom"] is False
        assert row["redirect_uri"] == row["default_redirect_uri"]
    finally:
        _clear_overrides()


async def test_put_redirect_uri_before_any_key_is_rejected(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        headers = await _admin(client, make_user)
        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": CUSTOM_ZERODHA},
            headers=headers,
        )
        assert resp.status_code == 400
    finally:
        _clear_overrides()


async def test_invalid_custom_redirects_are_rejected(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K"))
    try:
        headers = await _admin(client, make_user)
        for bad in (
            "https://tunnel.example.com/somewhere-else",  # never reaches the callback
            "https://tunnel.example.com/api/v1/broker-credentials/upstox/callback",  # other broker
            "ftp://tunnel.example.com/api/v1/broker-credentials/zerodha/callback",
            "/api/v1/broker-credentials/zerodha/callback",  # not absolute
            CUSTOM_ZERODHA + "?x=1",
        ):
            resp = await client.put(
                "/api/v1/broker-credentials/zerodha/redirect-uri",
                json={"redirect_uri": bad},
                headers=headers,
            )
            assert resp.status_code == 400, bad
        assert store.get_credentials("zerodha").redirect_uri_override is None
    finally:
        _clear_overrides()


async def test_reverse_proxy_prefix_is_allowed(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K"))
    prefixed = "https://example.com/tradingos/api/v1/broker-credentials/zerodha/callback"
    try:
        headers = await _admin(client, make_user)
        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": prefixed},
            headers=headers,
        )
        assert resp.status_code == 204
        assert store.get_credentials("zerodha").redirect_uri_override == prefixed
    finally:
        _clear_overrides()


async def test_saving_the_default_url_is_not_stored_as_custom(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K"))
    try:
        headers = await _admin(client, make_user)
        default = (await _status(client, headers))["zerodha"]["default_redirect_uri"]
        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": default},
            headers=headers,
        )
        assert resp.status_code == 204
        assert store.get_credentials("zerodha").redirect_uri_override is None
    finally:
        _clear_overrides()


async def test_put_redirect_uri_requires_system_administrator(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K"))
    try:
        headers = await _admin(client, make_user, role=Role.PORTFOLIO_MANAGER)
        resp = await client.put(
            "/api/v1/broker-credentials/zerodha/redirect-uri",
            json={"redirect_uri": CUSTOM_ZERODHA},
            headers=headers,
        )
        assert resp.status_code == 403
        assert store.get_credentials("zerodha").redirect_uri_override is None
    finally:
        _clear_overrides()


async def test_callback_lands_on_frontend_base_url(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://localhost:3003")
    get_settings.cache_clear()
    _override_store(tmp_path)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/callback?status=error", follow_redirects=False
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("http://localhost:3003/settings?")
    finally:
        _clear_overrides()
        monkeypatch.delenv("FRONTEND_BASE_URL")
        get_settings.cache_clear()
