"""Portfolio API tests (Phase 24, docs/phase20-old-vs-new-comparison.md
items 22 and 23): the advisory-only recommendation engine (real backtest
metrics decide each allocation action; no LLM key is configured in this
test environment, so every generation exercises the honest
`LlmRouterExhaustedError` fallback path -- the same "no provider
reachable" posture every other LLM-backed route in this codebase already
has real test coverage for) plus the cross-broker summary rollup.
"""

import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.live_position import LivePosition
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.portfolio_recommendation import PortfolioRecommendation
from src.models.strategy import StrategyStatus
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_allocatable_strategy(
    db_session_factory, *, name: str, status: str, metrics: dict | None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=name, objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        strategy.status = status
        if metrics is not None:
            db.add(
                BacktestRun(
                    strategy_version_id=version.id,
                    symbol=f"{name}SYM",
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 6, 1),
                    status=BacktestStatus.COMPLETED,
                    metrics=metrics,
                )
            )
        await db.commit()
        return strategy.id, version.id


async def test_generate_recommendation_derives_actions_from_real_backtest_metrics(
    client: AsyncClient, make_user, db_session_factory
):
    await _make_allocatable_strategy(
        db_session_factory,
        name="Winner",
        status=StrategyStatus.PAPER_TRADING.value,
        metrics={"sharpe": 2.1, "max_drawdown": 0.05, "total_return": 0.3},
    )
    await _make_allocatable_strategy(
        db_session_factory,
        name="Loser",
        status=StrategyStatus.LIVE.value,
        metrics={"sharpe": -0.5, "max_drawdown": 0.4, "total_return": -0.2},
    )
    await _make_allocatable_strategy(
        db_session_factory,
        name="Middling",
        status=StrategyStatus.LIVE_ELIGIBLE.value,
        metrics={"sharpe": 0.5, "max_drawdown": 0.1, "total_return": 0.05},
    )
    await _make_allocatable_strategy(
        db_session_factory,
        name="NoBacktestYet",
        status=StrategyStatus.PAPER_TRADING.value,
        metrics=None,
    )
    # Not allocatable at all -- must never appear in the response.
    await _make_allocatable_strategy(
        db_session_factory, name="StillIdeating", status=StrategyStatus.IDEATION.value, metrics=None
    )

    await make_user("pf-gen1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pf-gen1@example.com", "supersecret1")

    resp = await client.post("/api/v1/portfolio/recommendations/generate", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["llm_source"] == "fallback"  # no LLM provider configured in tests
    assert body["summary"]
    assert body["based_on"]["strategy_count"] == 4

    by_name = {a["strategy_name"]: a for a in body["allocations"]}
    assert set(by_name) == {"Winner", "Loser", "Middling", "NoBacktestYet"}
    assert by_name["Winner"]["action"] == "increase"
    assert by_name["Loser"]["action"] == "decrease"
    assert by_name["Middling"]["action"] == "hold"
    assert by_name["NoBacktestYet"]["action"] == "hold"
    assert "insufficient data" in by_name["NoBacktestYet"]["rationale"]


async def test_generate_recommendation_requires_operator_role(client: AsyncClient, make_user):
    await make_user("pf-gen2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pf-gen2@example.com", "supersecret1")

    resp = await client.post("/api/v1/portfolio/recommendations/generate", headers=_auth(token))
    assert resp.status_code == 403


async def test_list_and_get_recommendations_readable_by_every_role(
    client: AsyncClient, make_user, db_session_factory
):
    await make_user("pf-gen3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "pf-gen3@example.com", "supersecret1")
    gen_resp = await client.post(
        "/api/v1/portfolio/recommendations/generate", headers=_auth(admin_token)
    )
    recommendation_id = gen_resp.json()["id"]

    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"pf-list-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")

        list_resp = await client.get("/api/v1/portfolio/recommendations", headers=_auth(token))
        assert list_resp.status_code == 200, f"role {role} was denied list access"
        assert any(r["id"] == recommendation_id for r in list_resp.json())

        get_resp = await client.get(
            f"/api/v1/portfolio/recommendations/{recommendation_id}", headers=_auth(token)
        )
        assert get_resp.status_code == 200, f"role {role} was denied detail access"


async def test_get_recommendation_not_found_is_404(client: AsyncClient, make_user):
    await make_user("pf-404@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pf-404@example.com", "supersecret1")

    resp = await client.get(
        f"/api/v1/portfolio/recommendations/{uuid.uuid4()}", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_accept_recommendation_stamps_reviewer_and_writes_audit_entry(
    client: AsyncClient, make_user, db_session_factory
):
    await make_user("pf-accept1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pf-accept1@example.com", "supersecret1")

    gen_resp = await client.post("/api/v1/portfolio/recommendations/generate", headers=_auth(token))
    recommendation_id = gen_resp.json()["id"]

    accept_resp = await client.post(
        f"/api/v1/portfolio/recommendations/{recommendation_id}/accept",
        headers=_auth(token),
        json={"notes": "looks reasonable"},
    )
    assert accept_resp.status_code == 200
    body = accept_resp.json()
    assert body["status"] == "accepted"
    assert body["reviewed_by"] == "pf-accept1@example.com"
    assert body["reviewed_at"] is not None
    assert body["reviewer_notes"] == "looks reasonable"

    async with db_session_factory() as db:
        rec = await db.get(PortfolioRecommendation, uuid.UUID(recommendation_id))
        assert rec.status.value == "accepted"

        audit_result = await db.execute(
            select(AuditLog).where(AuditLog.entity_id == recommendation_id)
        )
        assert (
            audit_result.scalars().first() is not None
        ), "accepting a recommendation must be audited"


async def test_reject_recommendation_with_notes(client: AsyncClient, make_user):
    await make_user("pf-reject1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "pf-reject1@example.com", "supersecret1")

    gen_resp = await client.post("/api/v1/portfolio/recommendations/generate", headers=_auth(token))
    recommendation_id = gen_resp.json()["id"]

    reject_resp = await client.post(
        f"/api/v1/portfolio/recommendations/{recommendation_id}/reject",
        headers=_auth(token),
        json={"notes": "too aggressive right now"},
    )
    assert reject_resp.status_code == 200
    body = reject_resp.json()
    assert body["status"] == "rejected"
    assert body["reviewer_notes"] == "too aggressive right now"


async def test_accept_an_already_decided_recommendation_is_404(client: AsyncClient, make_user):
    await make_user("pf-double@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "pf-double@example.com", "supersecret1")

    gen_resp = await client.post("/api/v1/portfolio/recommendations/generate", headers=_auth(token))
    recommendation_id = gen_resp.json()["id"]

    first = await client.post(
        f"/api/v1/portfolio/recommendations/{recommendation_id}/accept",
        headers=_auth(token),
        json={},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/portfolio/recommendations/{recommendation_id}/reject",
        headers=_auth(token),
        json={},
    )
    assert second.status_code == 404


async def test_decide_recommendation_requires_operator_role(client: AsyncClient, make_user):
    await make_user("pf-decide-admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "pf-decide-admin@example.com", "supersecret1")
    gen_resp = await client.post(
        "/api/v1/portfolio/recommendations/generate", headers=_auth(admin_token)
    )
    recommendation_id = gen_resp.json()["id"]

    await make_user("pf-decide-ro@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    ro_token = await _login(client, "pf-decide-ro@example.com", "supersecret1")

    resp = await client.post(
        f"/api/v1/portfolio/recommendations/{recommendation_id}/accept",
        headers=_auth(ro_token),
        json={},
    )
    assert resp.status_code == 403


async def test_portfolio_summary_reports_real_counts_and_pnl(
    client: AsyncClient, make_user, db_session_factory
):
    strategy_id, version_id = await _make_allocatable_strategy(
        db_session_factory,
        name="SummaryStock",
        status=StrategyStatus.PAPER_TRADING.value,
        metrics=None,
    )

    async with db_session_factory() as db:
        subscription = await enroll_in_paper_trading(
            db, strategy_version_id=version_id, symbol="SUMMARYSTOCK"
        )
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="SUMMARYSTOCK",
                side="sell",
                order_group_id=uuid.uuid4(),
                requested_quantity=10,
                filled_quantity=10,
                avg_fill_price=105.0,
                fully_filled=True,
                realized_pnl=250.0,
            )
        )
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol="SUMMARYSTOCK", quantity=5, avg_cost=100.0
            )
        )
        db.add(
            LivePosition(
                strategy_id=strategy_id, symbol="SUMMARYSTOCK", quantity=10, avg_cost=100.0
            )
        )
        await db.commit()

    await make_user("pf-summary@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pf-summary@example.com", "supersecret1")

    resp = await client.get("/api/v1/portfolio/summary", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_strategy_count"] >= 1
    assert body["broker_configured"] is False
    assert body["available_margin"] is None
    assert body["used_margin"] is None
    assert body["paper_realized_pnl_today"] == 250.0
    assert body["paper_open_position_count"] == 1
    assert body["live_position_count"] == 1


async def test_portfolio_summary_readable_by_every_role(client: AsyncClient, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"pf-summary-role-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/portfolio/summary", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied summary access"


async def test_risk_metrics_computes_real_exposure_and_concentration(
    client: AsyncClient, make_user, db_session_factory
):
    strategy_id, version_id = await _make_allocatable_strategy(
        db_session_factory,
        name="RiskStock",
        status=StrategyStatus.LIVE.value,
        metrics=None,
    )

    async with db_session_factory() as db:
        subscription = await enroll_in_paper_trading(
            db, strategy_version_id=version_id, symbol="RISKPAPER"
        )
        # Paper exposure: |20 * 50| = 1000.
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol="RISKPAPER", quantity=20, avg_cost=50.0
            )
        )
        # Live exposure: |10 * 300| = 3000 -- the larger of the two, so it
        # must sort first and drive the concentration ratio.
        db.add(
            LivePosition(strategy_id=strategy_id, symbol="RISKLIVE", quantity=10, avg_cost=300.0)
        )
        await db.commit()

    await make_user("pf-risk1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pf-risk1@example.com", "supersecret1")

    resp = await client.get("/api/v1/portfolio/risk-metrics", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["open_position_count"] == 2
    assert body["total_exposure"] == 4000.0
    assert body["exposure_by_position"][0]["mode"] == "live"
    assert body["exposure_by_position"][0]["exposure"] == 3000.0
    assert body["exposure_by_position"][1]["exposure"] == 1000.0
    assert body["largest_position_concentration_pct"] == 75.0
    assert body["broker_configured"] is False
    assert body["margin_utilization_pct"] is None


async def test_risk_metrics_reports_null_concentration_with_no_open_positions(
    client: AsyncClient, make_user
):
    await make_user("pf-risk2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pf-risk2@example.com", "supersecret1")

    resp = await client.get("/api/v1/portfolio/risk-metrics", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["open_position_count"] == 0
    assert body["total_exposure"] == 0.0
    assert body["exposure_by_position"] == []
    assert body["largest_position_concentration_pct"] is None


async def test_risk_metrics_readable_by_every_role(client: AsyncClient, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"pf-risk-role-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/portfolio/risk-metrics", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied risk-metrics access"


async def test_risk_metrics_requires_authentication(client: AsyncClient):
    resp = await client.get("/api/v1/portfolio/risk-metrics")
    assert resp.status_code in (401, 403)
