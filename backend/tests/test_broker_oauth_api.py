"""Zerodha/Upstox OAuth completion tests (Settings redesign): login-URL
builders, the callback token exchange (checksum computation for Zerodha,
form-encoded grant for Upstox) against an injected httpx.MockTransport --
this sandbox has no live Zerodha/Upstox credentials or egress -- and that
the callback route accepts an unauthenticated GET (a real browser redirect
from the broker never carries an Authorization header).
"""

import hashlib

import httpx
from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.api.routes.broker_oauth import get_oauth_http_client
from src.brokers.base import BrokerCredentials
from src.core.roles import Role
from src.main import app
from src.security.secrets_store import SecretsStore


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_store(tmp_path):
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_broker_credentials_store] = lambda: store
    return store


def _override_http_client(handler):
    async def _fake_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    app.dependency_overrides[get_oauth_http_client] = _fake_client


def _clear_overrides():
    app.dependency_overrides.pop(get_broker_credentials_store, None)
    app.dependency_overrides.pop(get_oauth_http_client, None)


async def test_zerodha_login_url_requires_saved_api_key_first(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/login-url", headers=_auth(token)
        )
        assert resp.status_code == 400
    finally:
        _clear_overrides()


async def test_zerodha_login_url_requires_system_administrator(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K", api_secret="S"))
    try:
        await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "pm@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/login-url", headers=_auth(token)
        )
        assert resp.status_code == 403
    finally:
        _clear_overrides()


async def test_zerodha_login_url_builds_real_kite_connect_url(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="my-api-key", api_secret="S"))
    try:
        await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin2@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/login-url", headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["login_url"] == "https://kite.zerodha.com/connect/login?v=3&api_key=my-api-key"
        assert body["redirect_uri"].endswith("/api/v1/broker-credentials/zerodha/callback")

        # The redirect_uri is now persisted for the status endpoint to show.
        status_resp = await client.get("/api/v1/broker-credentials", headers=_auth(token))
        by_broker = {row["broker"]: row for row in status_resp.json()}
        assert by_broker["zerodha"]["redirect_uri"] == body["redirect_uri"]
        assert by_broker["zerodha"]["token_status"] == "never-connected"
    finally:
        _clear_overrides()


async def test_zerodha_callback_missing_request_token_redirects_with_error(client, tmp_path):
    _override_store(tmp_path)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/callback?status=error", follow_redirects=False
        )
        assert resp.status_code == 302
        assert "oauth=error" in resp.headers["location"]
    finally:
        _clear_overrides()


async def test_zerodha_callback_completes_real_checksum_exchange_and_stores_token(
    client, make_user, tmp_path, db_session_factory
):
    store = _override_store(tmp_path)
    store.set_credentials(
        "zerodha", BrokerCredentials(api_key="my-api-key", api_secret="my-api-secret")
    )

    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/token"
        assert request.headers["X-Kite-Version"] == "3"
        form = dict(pair.split("=") for pair in request.content.decode().split("&"))
        received.update(form)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"access_token": "real-access-token", "user_id": "AB1234"},
            },
        )

    _override_http_client(handler)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/callback?request_token=RT123&action=login&status=success",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oauth=success" in resp.headers["location"]

        # Verify the checksum sent was computed exactly as Kite Connect's
        # documented formula: SHA256(api_key + request_token + api_secret).
        expected_checksum = hashlib.sha256(b"my-api-keyRT123my-api-secret").hexdigest()
        assert received["checksum"] == expected_checksum
        assert received["request_token"] == "RT123"

        stored = store.get_credentials("zerodha")
        assert stored.access_token == "real-access-token"
        assert stored.token_status == "valid"
        assert stored.token_expires_at is not None
    finally:
        _clear_overrides()


async def test_zerodha_callback_rejected_exchange_redirects_with_error_not_500(client, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K", api_secret="S"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"status": "error", "message": "Invalid checksum"})

    _override_http_client(handler)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/zerodha/callback?request_token=bad&status=success",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oauth=error" in resp.headers["location"]
        assert "Invalid" in resp.headers["location"] or "checksum" in resp.headers["location"]
    finally:
        _clear_overrides()


async def test_upstox_login_url_includes_redirect_uri_and_response_type(
    client, make_user, tmp_path
):
    store = _override_store(tmp_path)
    store.set_credentials("upstox", BrokerCredentials(api_key="up-key", api_secret="up-secret"))
    try:
        await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin3@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/broker-credentials/upstox/login-url?duration=extended", headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "response_type=code" in body["login_url"]
        assert "client_id=up-key" in body["login_url"]
        assert "api.upstox.com/v2/login/authorization/dialog" in body["login_url"]

        status_resp = await client.get("/api/v1/broker-credentials", headers=_auth(token))
        by_broker = {row["broker"]: row for row in status_resp.json()}
        assert by_broker["upstox"]["token_duration"] == "extended"
    finally:
        _clear_overrides()


async def test_upstox_callback_completes_authorization_code_exchange(client, tmp_path):
    store = _override_store(tmp_path)
    store.set_credentials(
        "upstox",
        BrokerCredentials(
            api_key="up-key",
            api_secret="up-secret",
            redirect_uri="https://example.com/api/v1/broker-credentials/upstox/callback",
        ),
    )

    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/login/authorization/token"
        form = dict(pair.split("=") for pair in request.content.decode().split("&"))
        received.update(form)
        return httpx.Response(200, json={"access_token": "up-real-token", "token_type": "Bearer"})

    _override_http_client(handler)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/upstox/callback?code=AUTHCODE", follow_redirects=False
        )
        assert resp.status_code == 302
        assert "oauth=success" in resp.headers["location"]

        assert received["grant_type"] == "authorization_code"
        assert received["code"] == "AUTHCODE"
        assert received["client_id"] == "up-key"

        stored = store.get_credentials("upstox")
        assert stored.access_token == "up-real-token"
    finally:
        _clear_overrides()


async def test_upstox_callback_error_param_redirects_without_calling_upstox(client, tmp_path):
    _override_store(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never call Upstox when the callback carries an error")

    _override_http_client(handler)
    try:
        resp = await client.get(
            "/api/v1/broker-credentials/upstox/callback?error=access_denied", follow_redirects=False
        )
        assert resp.status_code == 302
        assert "oauth=error" in resp.headers["location"]
    finally:
        _clear_overrides()
