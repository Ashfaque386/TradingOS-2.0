"""Shadow Mode API tests (Build Spec §13): a check against a broker with
no configured credentials is rejected before any adapter is even built,
and a real check's HTTP response carries the same honest
sandbox-vs-local-only differentiation as the underlying orchestration
function.
"""

from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.broker_credentials import get_broker_credentials_store
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


def _override_store(tmp_path) -> SecretsStore:
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_broker_credentials_store] = lambda: store
    return store


def _clear_override():
    app.dependency_overrides.pop(get_broker_credentials_store, None)


async def test_check_without_configured_credentials_is_rejected(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("risk@example.com", "supersecret1", Role.RISK_MANAGER)
        token = await _login(client, "risk@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/shadow-mode/check",
            json={"broker": "zerodha", "symbol": "INFY", "side": "buy", "quantity": 10},
            headers=_auth(token),
        )
        assert resp.status_code == 400
    finally:
        _clear_override()


async def test_check_unknown_broker_is_404(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("risk2@example.com", "supersecret1", Role.RISK_MANAGER)
        token = await _login(client, "risk2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/shadow-mode/check",
            json={"broker": "not-a-broker", "symbol": "INFY", "side": "buy", "quantity": 10},
            headers=_auth(token),
        )
        assert resp.status_code == 404
    finally:
        _clear_override()


async def test_check_requires_operator_role(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("auditor2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "auditor2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/shadow-mode/check",
            json={"broker": "zerodha", "symbol": "INFY", "side": "buy", "quantity": 10},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_zerodha_check_with_credentials_reports_local_payload_only(
    client, make_user, tmp_path
):
    store = _override_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
    try:
        await make_user("risk3@example.com", "supersecret1", Role.RISK_MANAGER)
        token = await _login(client, "risk3@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/shadow-mode/check",
            json={"broker": "zerodha", "symbol": "INFY", "side": "buy", "quantity": 10},
            headers=_auth(token),
        )
        # No live api.kite.trade egress in this sandbox -- ZerodhaKiteAdapter
        # built by the route has no injected mock transport, so a real
        # network attempt would raise/timeout. Because Zerodha has no
        # sandbox, run_shadow_order_check never calls place_order for it at
        # all, so this succeeds without ever touching the network.
        assert resp.status_code == 201
        body = resp.json()
        assert body["broker_name"] == "zerodha"
        assert body["has_sandbox"] is False
        assert body["confidence"] == "local_payload_only"
        assert body["broker_response"] is None

        list_resp = await client.get("/api/v1/shadow-mode/runs", headers=_auth(token))
        assert list_resp.status_code == 200
        assert any(row["id"] == body["id"] for row in list_resp.json())
    finally:
        _clear_override()
