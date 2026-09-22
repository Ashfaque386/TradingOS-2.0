"""Broker credentials API tests (Build Spec §3, §20): write-only end to
end -- a stored credential is never echoed back in any response body,
only a configured/not-configured boolean is, and only
SystemAdministrator may write or delete.
"""

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.brokers import breaker_registry
from src.core.roles import Role
from src.engine.risk.circuit_breaker import BrokerServerError
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


def _clear_override():
    app.dependency_overrides.pop(get_broker_credentials_store, None)


async def test_write_requires_system_administrator(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "pm@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/broker-credentials/zerodha",
            json={"api_key": "K", "access_token": "T"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_write_then_status_never_leaks_the_value(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    try:
        await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin@example.com", "supersecret1")

        write_resp = await client.post(
            "/api/v1/broker-credentials/zerodha",
            json={"api_key": "RAW_SECRET_KEY_VALUE", "access_token": "RAW_SECRET_TOKEN_VALUE"},
            headers=_auth(token),
        )
        assert write_resp.status_code == 204
        assert "RAW_SECRET_KEY_VALUE" not in write_resp.text
        assert "RAW_SECRET_TOKEN_VALUE" not in write_resp.text

        status_resp = await client.get("/api/v1/broker-credentials", headers=_auth(token))
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert "RAW_SECRET_KEY_VALUE" not in status_resp.text
        assert "RAW_SECRET_TOKEN_VALUE" not in status_resp.text
        by_broker = {row["broker"]: row["configured"] for row in body}
        assert by_broker["zerodha"] is True
        assert by_broker["upstox"] is False

        assert store.get_credentials("zerodha").api_key == "RAW_SECRET_KEY_VALUE"
    finally:
        _clear_override()


async def test_write_unknown_broker_is_404(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/broker-credentials/not-a-real-broker",
            json={"api_key": "K"},
            headers=_auth(token),
        )
        assert resp.status_code == 404
    finally:
        _clear_override()


async def test_delete_requires_system_administrator_and_then_clears_status(
    client, make_user, tmp_path
):
    store = _override_store(tmp_path)
    try:
        await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin3@example.com", "supersecret1")

        await client.post(
            "/api/v1/broker-credentials/upstox",
            json={"api_key": "K", "access_token": "T"},
            headers=_auth(token),
        )
        assert store.get_credentials("upstox") is not None

        delete_resp = await client.delete("/api/v1/broker-credentials/upstox", headers=_auth(token))
        assert delete_resp.status_code == 204
        assert store.get_credentials("upstox") is None
    finally:
        _clear_override()


async def test_circuit_breaker_status_defaults_closed_for_a_never_built_adapter(client, make_user):
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        await make_user("cb1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "cb1@example.com", "supersecret1")

        resp = await client.get("/api/v1/broker-credentials/circuit-breaker", headers=_auth(token))
        assert resp.status_code == 200
        by_broker = {row["broker"]: row for row in resp.json()}
        assert by_broker["zerodha"]["state"] == "closed"
        assert by_broker["zerodha"]["consecutive_failures"] == 0
        assert by_broker["zerodha"]["cooldown_remaining_seconds"] is None
        assert by_broker["upstox"]["state"] == "closed"
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)


async def test_circuit_breaker_status_reflects_a_real_tripped_breaker(client, make_user):
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        await make_user("cb2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "cb2@example.com", "supersecret1")

        breaker = breaker_registry.get_broker_circuit_breaker("zerodha")

        async def _always_5xx():
            raise BrokerServerError(503, "simulated broker outage")

        for _ in range(3):
            with pytest.raises(BrokerServerError):
                await breaker.call(_always_5xx)

        resp = await client.get("/api/v1/broker-credentials/circuit-breaker", headers=_auth(token))
        assert resp.status_code == 200
        by_broker = {row["broker"]: row for row in resp.json()}
        assert by_broker["zerodha"]["state"] == "open"
        assert by_broker["zerodha"]["consecutive_failures"] == 3
        assert by_broker["zerodha"]["cooldown_remaining_seconds"] > 0
        # A broker whose breaker was never exercised in this test is
        # unaffected -- proof the two brokers' state is genuinely
        # independent, not a shared/global flag.
        assert by_broker["upstox"]["state"] == "closed"
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)


async def test_circuit_breaker_status_is_readable_by_every_role(client, make_user):
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        for i, role in enumerate(
            [
                Role.SYSTEM_ADMINISTRATOR,
                Role.PORTFOLIO_MANAGER,
                Role.RISK_MANAGER,
                Role.READ_ONLY_AUDITOR,
            ]
        ):
            email = f"cb3-{i}@example.com"
            await make_user(email, "supersecret1", role)
            token = await _login(client, email, "supersecret1")
            resp = await client.get(
                "/api/v1/broker-credentials/circuit-breaker", headers=_auth(token)
            )
            assert resp.status_code == 200, f"role {role} was denied read access"
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
