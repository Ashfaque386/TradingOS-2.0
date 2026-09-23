"""System Vitals API tests (Phase 17 real-world testing pass): GET
/api/v1/system/vitals over real HTTP, RBAC included. Deep coverage of
each figure's own semantics lives in tests/test_observability_vitals.py
-- these tests confirm the route wires them together correctly and that
every role can read it.
"""

from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_system_vitals_returns_the_full_documented_shape_over_real_http(
    client: AsyncClient, make_user
):
    await make_user("vitals-sys1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "vitals-sys1@example.com", "supersecret1")

    resp = await client.get("/api/v1/system/vitals", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"host", "llm", "latency", "market", "as_of"}
    assert set(body["host"].keys()) == {
        "cpu_percent",
        "memory_percent",
        "memory_used_mb",
        "uptime_seconds",
    }
    assert "active_provider" in body["llm"]
    assert isinstance(body["llm"]["token_usage_today"], dict)
    assert isinstance(body["llm"]["provider_health"], list)
    assert body["llm"]["provider_health"], "every configured provider must be listed"
    for entry in body["llm"]["provider_health"]:
        assert set(entry.keys()) == {
            "provider",
            "last_failure_at",
            "last_success_at",
            "served_as_fallback",
        }
    assert set(body["latency"].keys()) == {
        "order_dispatch_p50_ms",
        "order_dispatch_p95_ms",
        "window",
        "budget_ms",
    }
    assert body["latency"]["window"] == "5m"
    # This sandbox has no reachable Prometheus (Docker Hub pulls are
    # blocked, same as every other unverified compose service) -- a real
    # attempt, not a fabricated percentile, so both come back null.
    assert body["latency"]["order_dispatch_p50_ms"] is None
    assert body["latency"]["order_dispatch_p95_ms"] is None
    assert isinstance(body["market"]["is_open"], bool)
    assert "T" in body["market"]["next_event_at"]
    assert "T" in body["as_of"]


async def test_system_vitals_readable_by_every_role(client: AsyncClient, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"vitals-sys2-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/system/vitals", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied read access"


async def test_system_vitals_requires_authentication(client: AsyncClient):
    resp = await client.get("/api/v1/system/vitals")
    assert resp.status_code in (401, 403)
