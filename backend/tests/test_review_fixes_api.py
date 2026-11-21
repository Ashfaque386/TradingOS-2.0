"""What the 2026-11-21 review found in the API, fixed: no per-route latency metric, the Portfolio
page asking an expired broker for its margin on every load, and webhook deliveries and sign-ins
written to the audit trail as "anonymous"."""

import asyncio
import json
import uuid

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

import src.api.routes.portfolio as portfolio_routes
from src.brokers.base import BrokerAuthError, MarginInfo
from src.core.config import get_settings
from src.core.roles import Role
from src.main import app
from src.models.audit_log import AuditLog
from src.notifications.alertmanager_token import ensure_token
from src.observability.audit_middleware import AuditLoggingMiddleware
from src.observability.request_metrics import UNMATCHED_ROUTE

# --- request latency ------------------------------------------------------------------------------


def _count(method: str, route: str, status_class: str) -> float:
    value = REGISTRY.get_sample_value(
        "tradingos_http_request_duration_seconds_count",
        {"method": method, "route": route, "status_class": status_class},
    )
    return value or 0.0


async def test_a_request_is_timed_under_its_route_template_not_its_path(client):
    template = "/api/v1/strategies/{strategy_id}"
    before = _count("GET", template, "4xx")

    response = await client.get(f"/api/v1/strategies/{uuid.uuid4()}")  # no token: 401

    assert response.status_code == 401
    assert _count("GET", template, "4xx") == before + 1


async def test_a_request_matching_no_route_is_one_unmatched_series(client):
    before = _count("GET", UNMATCHED_ROUTE, "4xx")

    await client.get(f"/api/v1/no-such-thing/{uuid.uuid4()}")
    await client.get("/also/not/a/route")

    assert _count("GET", UNMATCHED_ROUTE, "4xx") == before + 2


async def test_prometheus_scraping_metrics_is_not_counted(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert 'route="/metrics"' not in response.text


async def test_a_public_route_answering_200_is_counted_as_2xx(client):
    route = "/api/v1/auth/registration-status"
    before = _count("GET", route, "2xx")

    await client.get(route)

    assert _count("GET", route, "2xx") == before + 1


# --- the Portfolio page's broker margin ----------------------------------------------------------


class _CountingBroker:
    broker_name = "zerodha"

    def __init__(self, *, refuse: bool = False, delay: float = 0.0) -> None:
        self.calls = 0
        self._refuse = refuse
        self._delay = delay

    async def get_margin(self) -> MarginInfo:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._refuse:
            raise BrokerAuthError("token expired")
        return MarginInfo(available_margin=900.0, used_margin=100.0, raw={})


async def test_the_two_parallel_page_calls_share_one_broker_call():
    broker = _CountingBroker(delay=0.05)

    first, second = await asyncio.gather(
        portfolio_routes.cached_margin(broker),  # type: ignore[arg-type]
        portfolio_routes.cached_margin(broker),  # type: ignore[arg-type]
    )
    again = await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]

    assert broker.calls == 1
    assert first.used_margin == second.used_margin == again.used_margin == 100.0


async def test_a_refused_token_is_not_asked_again_until_its_back_off_ends(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(portfolio_routes.time, "monotonic", lambda: clock[0])
    broker = _CountingBroker(refuse=True)

    for _ in range(3):
        with pytest.raises(BrokerAuthError):
            await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]
    assert broker.calls == 1

    clock[0] += portfolio_routes.MARGIN_AUTH_FAILURE_TTL_SECONDS - 1
    with pytest.raises(BrokerAuthError):
        await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]
    assert broker.calls == 1

    clock[0] += 2
    with pytest.raises(BrokerAuthError):
        await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]
    assert broker.calls == 2


async def test_a_margin_is_asked_again_once_it_is_30_seconds_old(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(portfolio_routes.time, "monotonic", lambda: clock[0])
    broker = _CountingBroker()

    await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]
    clock[0] += portfolio_routes.MARGIN_TTL_SECONDS - 1
    await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]
    clock[0] += 2
    await portfolio_routes.cached_margin(broker)  # type: ignore[arg-type]

    assert broker.calls == 2


async def test_the_summary_route_reads_the_margin_through_the_cache(client, make_user):
    broker = _CountingBroker()
    app.dependency_overrides[portfolio_routes.get_portfolio_broker_adapter] = lambda: broker
    try:
        await make_user("pm@example.com", "a-long-test-password", Role.PORTFOLIO_MANAGER)
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "pm@example.com", "password": "a-long-test-password"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        summary = await client.get("/api/v1/portfolio/summary", headers=headers)
        risk = await client.get("/api/v1/portfolio/risk-metrics", headers=headers)
    finally:
        app.dependency_overrides.pop(portfolio_routes.get_portfolio_broker_adapter, None)

    assert summary.status_code == risk.status_code == 200
    assert summary.json()["used_margin"] == 100.0
    assert risk.json()["margin_utilization_pct"] == 10.0
    assert broker.calls == 1


# --- who the audit trail names -------------------------------------------------------------------


def test_a_named_actor_wins_over_a_signed_in_user_and_anonymous_is_the_last_resort():
    class _User:
        email = "someone@example.com"

    actor = AuditLoggingMiddleware._extract_actor
    both = {"state": {"audit_actor": "alertmanager", "current_user": _User()}}
    assert actor(both) == "alertmanager"
    assert actor({"state": {"current_user": _User()}}) == "someone@example.com"
    assert actor({"state": {}}) == "anonymous"


async def _newest_audit_row(db_session_factory, action: str) -> AuditLog:
    async with db_session_factory() as db:
        return (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == action)
                .order_by(AuditLog.sequence.desc())
                .limit(1)
            )
        ).scalar_one()


async def test_an_alertmanager_delivery_is_recorded_as_alertmanager(
    client, db_session_factory, tmp_path, monkeypatch
):
    path = tmp_path / "alertmanager_token" / "webhook_token"
    monkeypatch.setenv("ALERTMANAGER_TOKEN_PATH", str(path))
    get_settings.cache_clear()
    try:
        token = ensure_token(path)
        response = await client.post(
            "/api/v1/webhooks/alertmanager",
            content=json.dumps({"version": "4", "status": "resolved", "alerts": []}),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    row = await _newest_audit_row(db_session_factory, "POST /api/v1/webhooks/alertmanager")
    assert row.actor == "alertmanager"


async def test_a_sign_in_is_recorded_under_the_account_that_signed_in(
    client, db_session_factory, make_user
):
    await make_user("auditor@example.com", "a-long-test-password", Role.READ_ONLY_AUDITOR)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "auditor@example.com", "password": "a-long-test-password"},
    )

    assert response.status_code == 200
    row = await _newest_audit_row(db_session_factory, "POST /api/v1/auth/login")
    assert row.actor == "auditor@example.com"
