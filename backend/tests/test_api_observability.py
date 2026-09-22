"""System Vitals API tests (Phase 16 audit follow-up E): the endpoint
reads the *same* process-global Prometheus Counter/Histogram objects
`/metrics` already exposes -- so these tests use distinct, made-up
label values (never "zerodha"/"upstox"/a real provider name, all of
which other test files in this suite also touch) to get deterministic
assertions without needing a before/after diff against shared state.
"""

from httpx import AsyncClient

from src.core.roles import Role
from src.observability.metrics import (
    build_vitals_summary,
    llm_token_usage_total,
    order_dispatch_budget_breached_total,
    order_dispatch_latency_seconds,
)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_build_vitals_summary_includes_a_since_timestamp_not_a_today_label():
    summary = build_vitals_summary()
    assert "since" in summary
    assert isinstance(summary["since"], str)
    assert "T" in summary["since"]  # a real ISO datetime, not a bare date


def test_build_vitals_summary_reports_real_token_usage():
    llm_token_usage_total.labels(provider="vitals-test-provider", token_type="prompt").inc(5)
    llm_token_usage_total.labels(provider="vitals-test-provider", token_type="completion").inc(2)

    summary = build_vitals_summary()
    entries = {
        (e["provider"], e["token_type"]): e["count"]
        for e in summary["llm_token_usage"]
        if e["provider"] == "vitals-test-provider"
    }
    assert entries[("vitals-test-provider", "prompt")] == 5
    assert entries[("vitals-test-provider", "completion")] == 2


def test_build_vitals_summary_never_fabricates_an_average_for_zero_dispatches():
    # .labels(...) alone (no .observe()) registers the child with a real,
    # honest zero count -- distinct from "this broker was never queried".
    order_dispatch_latency_seconds.labels(broker="vitals-test-zero-broker")

    summary = build_vitals_summary()
    entry = next(e for e in summary["order_dispatch"] if e["broker"] == "vitals-test-zero-broker")
    assert entry["dispatch_count"] == 0
    assert entry["avg_latency_ms"] is None
    assert entry["budget_breaches"] == 0


def test_build_vitals_summary_computes_a_real_average_latency_and_budget_breaches():
    broker = "vitals-test-active-broker"
    order_dispatch_latency_seconds.labels(broker=broker).observe(0.1)
    order_dispatch_latency_seconds.labels(broker=broker).observe(0.3)
    order_dispatch_budget_breached_total.labels(broker=broker).inc(2)

    summary = build_vitals_summary()
    entry = next(e for e in summary["order_dispatch"] if e["broker"] == broker)
    assert entry["dispatch_count"] == 2
    assert entry["avg_latency_ms"] == 200.0  # (0.1 + 0.3) / 2 seconds -> 200ms
    assert entry["budget_breaches"] == 2


async def test_vitals_endpoint_returns_the_same_shape_over_real_http(client, make_user):
    llm_token_usage_total.labels(provider="vitals-http-provider", token_type="prompt").inc(3)

    await make_user("vitals1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "vitals1@example.com", "supersecret1")

    resp = await client.get("/api/v1/observability/vitals", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert "since" in body
    entries = [e for e in body["llm_token_usage"] if e["provider"] == "vitals-http-provider"]
    assert entries == [{"provider": "vitals-http-provider", "token_type": "prompt", "count": 3}]


async def test_vitals_endpoint_readable_by_every_role(client, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"vitals2-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/observability/vitals", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied read access"
