"""The automatic kill switch over HTTP (Phase 3): evaluate now, the equity history, and the
dual-control risk limit's new catalog and rejection.
"""

import pytest
from httpx import AsyncClient

from src.core.redis_client import get_redis
from src.core.roles import Role as UserRole
from src.main import app
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


@pytest.fixture(autouse=True)
def _isolate_redis_dependency(redis_client):
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_redis, None)


async def _login(client: AsyncClient, make_user, email: str, role: UserRole) -> dict:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _capital(db_session_factory, amount=100_000.0):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="RiskMonitorDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        db.add(
            PaperTradingSubscription(
                strategy_version_id=version.id,
                symbol="RMON",
                builtin_strategy="always_long",
                initial_capital=amount,
            )
        )
        await db.commit()


# ---- evaluate now


async def test_evaluating_measures_the_equity_and_stores_the_reading(
    client, make_user, db_session_factory
):
    await _capital(db_session_factory)
    headers = await _login(client, make_user, "rm-eval@example.com", UserRole.RISK_MANAGER)

    resp = await client.post("/api/v1/kill-switch/paper/evaluate", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "ok"
    assert body["snapshot"]["equity"] == 100_000.0
    assert body["snapshot"]["peak_equity"] == 100_000.0
    assert body["snapshot"]["drawdown_pct"] == 0.0
    assert body["kill_switch"]["tripped"] is False
    assert body["kill_switch"]["threshold_pct"] == 15.0

    history = await client.get("/api/v1/equity/paper", headers=headers)
    assert [s["equity"] for s in history.json()["snapshots"]] == [100_000.0]


async def test_evaluating_when_equity_cannot_be_measured_says_so_and_trips_nothing(
    client, make_user
):
    headers = await _login(client, make_user, "rm-gap@example.com", UserRole.RISK_MANAGER)

    resp = await client.post("/api/v1/kill-switch/paper/evaluate", headers=headers)

    body = resp.json()
    assert body["outcome"] == "cannot_evaluate"
    assert body["snapshot"]["equity"] is None
    assert "no paper subscription has any capital" in body["snapshot"]["note"]
    assert body["kill_switch"]["tripped"] is False


async def test_evaluating_live_without_a_starting_capital_cannot_be_judged(client, make_user):
    headers = await _login(client, make_user, "rm-live@example.com", UserRole.RISK_MANAGER)

    resp = await client.post("/api/v1/kill-switch/live/evaluate", headers=headers)

    assert resp.json()["outcome"] == "cannot_evaluate"
    assert "LIVE_STARTING_CAPITAL is not set" in resp.json()["snapshot"]["note"]


@pytest.mark.parametrize("role", [UserRole.PORTFOLIO_MANAGER, UserRole.READ_ONLY_AUDITOR])
async def test_only_risk_roles_may_evaluate(client, make_user, role):
    headers = await _login(client, make_user, f"nope-{role.value}@example.com", role)

    resp = await client.post("/api/v1/kill-switch/paper/evaluate", headers=headers)

    assert resp.status_code == 403


# ---- the equity history


async def test_the_history_is_oldest_first_with_the_limit_in_force(
    client, make_user, db_session_factory
):
    await _capital(db_session_factory)
    headers = await _login(client, make_user, "rm-hist@example.com", UserRole.RISK_MANAGER)
    for _ in range(3):
        await client.post("/api/v1/kill-switch/paper/evaluate", headers=headers)

    resp = await client.get("/api/v1/equity/paper?limit=2", headers=headers)

    body = resp.json()
    assert body["mode"] == "paper"
    assert body["threshold_pct"] == 15.0
    times = [s["taken_at"] for s in body["snapshots"]]
    assert len(times) == 2
    assert times == sorted(times)


async def test_the_history_is_per_mode_and_open_to_every_role(client, make_user):
    headers = await _login(client, make_user, "aud-hist@example.com", UserRole.READ_ONLY_AUDITOR)

    resp = await client.get("/api/v1/equity/live", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["snapshots"] == []


# ---- risk limits: catalog, validation, rejection


async def test_the_catalog_lists_the_limits_with_what_is_in_force(client, make_user):
    headers = await _login(client, make_user, "cat@example.com", UserRole.READ_ONLY_AUDITOR)

    resp = await client.get("/api/v1/risk-limits/catalog", headers=headers)

    entries = {e["name"]: e for e in resp.json()}
    assert set(entries) == {"max_drawdown_pct", "ws_latency_ms"}
    drawdown = entries["max_drawdown_pct"]
    assert drawdown["enforced"] is True
    assert (drawdown["default"], drawdown["effective"], drawdown["applied"]) == (15.0, 15.0, False)
    assert (drawdown["minimum"], drawdown["maximum"], drawdown["unit"]) == (0.5, 100.0, "%")
    assert entries["ws_latency_ms"]["enforced"] is False


async def test_the_catalog_shows_an_applied_value_as_in_force(client, make_user):
    admin = await _login(client, make_user, "cat-a@example.com", UserRole.SYSTEM_ADMINISTRATOR)
    risk = await _login(client, make_user, "cat-b@example.com", UserRole.RISK_MANAGER)
    staged = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 8.0},
        headers=admin,
    )
    change_id = staged.json()["id"]
    await client.post(f"/api/v1/risk-limits/{change_id}/confirm", headers=risk)
    await client.post(f"/api/v1/risk-limits/{change_id}/apply", headers=risk)

    resp = await client.get("/api/v1/risk-limits/catalog", headers=admin)

    drawdown = next(e for e in resp.json() if e["name"] == "max_drawdown_pct")
    assert (drawdown["effective"], drawdown["applied"]) == (8.0, True)


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("not_a_limit", 5.0, "unknown risk limit"),
        ("max_drawdown_pct", 0.1, "between 0.5 and 100"),
        ("max_drawdown_pct", 150.0, "between 0.5 and 100"),
    ],
)
async def test_staging_an_unknown_limit_or_a_value_out_of_range_is_refused(
    client, make_user, name, value, fragment
):
    headers = await _login(client, make_user, "stage-bad@example.com", UserRole.RISK_MANAGER)

    resp = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": name, "proposed_value": value},
        headers=headers,
    )

    assert resp.status_code == 422
    assert fragment in resp.json()["detail"]


async def test_a_change_can_be_rejected_with_who_when_and_why_and_changes_nothing(
    client, make_user
):
    staging = await _login(client, make_user, "rej-a@example.com", UserRole.RISK_MANAGER)
    other = await _login(client, make_user, "rej-b@example.com", UserRole.SYSTEM_ADMINISTRATOR)
    staged = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 40.0, "reason": "loosen"},
        headers=staging,
    )
    change_id = staged.json()["id"]
    assert staged.json()["staged_by_label"] == "rej-a@example.com"

    resp = await client.post(
        f"/api/v1/risk-limits/{change_id}/reject", json={"reason": " too loose "}, headers=other
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["rejection_reason"] == "too loose"
    assert body["rejected_by_label"] == "rej-b@example.com"
    assert body["rejected_at"] is not None
    applied = await client.post(f"/api/v1/risk-limits/{change_id}/apply", headers=other)
    assert applied.status_code == 409  # a rejected change can never be applied
    listing = await client.get("/api/v1/risk-limits", headers=other)
    assert listing.json() == []  # nothing was applied


async def test_the_person_who_staged_a_change_can_cancel_it(client, make_user):
    headers = await _login(client, make_user, "cancel@example.com", UserRole.RISK_MANAGER)
    staged = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 10.0},
        headers=headers,
    )

    resp = await client.post(
        f"/api/v1/risk-limits/{staged.json()['id']}/reject", json={}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] is None


async def test_a_change_that_is_not_pending_cannot_be_rejected(client, make_user):
    headers = await _login(client, make_user, "rej-twice@example.com", UserRole.RISK_MANAGER)
    staged = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 10.0},
        headers=headers,
    )
    change_id = staged.json()["id"]
    await client.post(f"/api/v1/risk-limits/{change_id}/reject", json={}, headers=headers)

    again = await client.post(f"/api/v1/risk-limits/{change_id}/reject", json={}, headers=headers)
    missing = await client.post(
        "/api/v1/risk-limits/00000000-0000-4000-8000-000000000000/reject", json={}, headers=headers
    )

    assert again.status_code == 409
    assert missing.status_code == 404


async def test_rejecting_requires_a_risk_role(client, make_user):
    headers = await _login(client, make_user, "rej-pm@example.com", UserRole.PORTFOLIO_MANAGER)

    resp = await client.post(
        "/api/v1/risk-limits/00000000-0000-4000-8000-000000000000/reject", json={}, headers=headers
    )

    assert resp.status_code == 403


async def test_the_change_list_names_the_people_by_e_mail(client, make_user):
    headers = await _login(client, make_user, "list-names@example.com", UserRole.RISK_MANAGER)
    await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 10.0},
        headers=headers,
    )

    resp = await client.get("/api/v1/risk-limits/changes", headers=headers)

    assert resp.json()[0]["staged_by_label"] == "list-names@example.com"
    assert resp.json()[0]["confirmed_by_label"] is None
