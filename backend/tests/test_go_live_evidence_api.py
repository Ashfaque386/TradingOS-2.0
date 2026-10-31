"""Go-Live sign-off over HTTP (Phase 4): the gate is judged on evidence the system derived from its
own records; typed numbers are an explicit, reasoned, audited override.
"""

import uuid
from datetime import UTC, datetime, time, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from src.core.roles import Role
from src.data.nse_calendar import is_nse_trading_day
from src.engine.paper_trading.market_hours import IST
from src.models.audit_log import AuditLog
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeRun

OVERRIDE = {
    "num_trades": 50,
    "calendar_days_running": 30,
    "clean_shadow_mode_streak_days": 15,
    "live_win_rate": 0.55,
    "backtest_win_rate": 0.5,
    "reason": "operator reviewed the desk by hand and accepts the numbers",
}


async def _login(client: AsyncClient, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _strategy_in_paper_trading(client, admin, risk, name) -> tuple[str, str]:
    """A strategy promoted to PaperTrading with the live-eligibility request already approved."""
    created = (
        await client.post(
            "/api/v1/strategies", json={"name": name, "objective": "o"}, headers=admin
        )
    ).json()
    strategy_id = created["id"]
    promotion = (
        await client.post(f"/api/v1/strategies/{strategy_id}/request-promotion", headers=admin)
    ).json()
    await client.post(
        f"/api/v1/approvals/{promotion['id']}/decide", json={"approve": True}, headers=risk
    )
    promoted = await client.post(f"/api/v1/strategies/{strategy_id}/promote", headers=admin)
    assert promoted.json()["status"] == "PaperTrading"
    live_request = (
        await client.post(
            f"/api/v1/strategies/{strategy_id}/request-live-eligibility", headers=risk
        )
    ).json()
    await client.post(
        f"/api/v1/approvals/{live_request['id']}/decide", json={"approve": True}, headers=risk
    )
    return strategy_id, created["versions"][0]["id"]


async def _record_passing_evidence(db_session_factory, strategy_id, version_id):
    """30 closing fills on real prices over 25 days, a real-data backtest at a similar win rate, and
    ten clean trading days of dry runs: everything the gate asks for."""
    now = datetime.now(UTC)
    async with db_session_factory() as db:
        sub = PaperTradingSubscription(
            strategy_version_id=uuid.UUID(version_id), symbol="PASS", builtin_strategy="always_long"
        )
        db.add(sub)
        await db.flush()
        for i in range(31):
            db.add(
                PaperFill(
                    subscription_id=sub.id,
                    symbol="PASS",
                    side="sell",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=1,
                    filled_quantity=1,
                    avg_fill_price=100.0,
                    fully_filled=True,
                    realized_pnl=10.0 if i % 2 == 0 else -5.0,
                    data_source="mixed",
                    created_at=now - timedelta(days=25, hours=-i),
                )
            )
        db.add(
            BacktestRun(
                strategy_version_id=uuid.UUID(version_id),
                symbol="PASS",
                start_date=now.date() - timedelta(days=365),
                end_date=now.date(),
                status=BacktestStatus.COMPLETED,
                data_source="real",
                metrics={"win_rate": 0.5},
            )
        )
        # Ten real NSE trading days back from today (holidays skipped), as the gate counts them.
        day = now.astimezone(IST).date()
        recorded = 0
        while recorded < 10:
            if is_nse_trading_day(day):
                db.add(
                    ShadowModeRun(
                        broker_name="upstox",
                        has_sandbox=True,
                        confidence="real_sandbox_dry_run",
                        symbol="PASS",
                        side="buy",
                        quantity=1,
                        order_payload={},
                        strategy_id=uuid.UUID(strategy_id),
                        outcome="ok",
                        created_at=datetime.combine(day, time(4, 0), tzinfo=UTC),
                    )
                )
                recorded += 1
            day -= timedelta(days=1)
        await db.commit()


# ---- reading the evidence


async def test_the_evidence_of_a_strategy_with_no_history_says_what_is_missing(client, make_user):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev-admin@example.com")
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "EvEmpty", "objective": "o"}, headers=admin
        )
    ).json()

    resp = await client.get(f"/api/v1/strategies/{strategy['id']}/go-live-evidence", headers=admin)

    assert resp.status_code == 200
    body = resp.json()
    assert body["inputs"] == {
        "num_trades": 0,
        "calendar_days_running": 0,
        "clean_shadow_mode_streak_days": 0,
        "live_win_rate": None,
        "backtest_win_rate": None,
    }
    assert body["thresholds"] == {
        "min_trades": 30,
        "min_calendar_days": 21,
        "min_clean_shadow_days": 10,
        "max_win_rate_divergence_pp": 20.0,
    }
    assert body["eligible"] is False
    assert set(body["checks"]) == {
        "min_trades",
        "min_calendar_days",
        "clean_shadow_streak",
        "win_rate_divergence",
    }
    assert {n["input"] for n in body["not_derived"]} >= {"num_trades", "backtest_win_rate"}
    assert body["counted_fills"] == 0


async def test_the_evidence_is_readable_by_every_role_and_404s_for_an_unknown_strategy(
    client, make_user
):
    auditor = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "ev-aud@example.com")

    missing = await client.get(
        f"/api/v1/strategies/{uuid.uuid4()}/go-live-evidence", headers=auditor
    )
    anonymous = await client.get(f"/api/v1/strategies/{uuid.uuid4()}/go-live-evidence")

    assert missing.status_code == 404
    assert anonymous.status_code in (401, 403)


async def test_recorded_evidence_is_derived_and_passes_the_gate(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev2-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev2-risk@example.com")
    strategy_id, version_id = await _strategy_in_paper_trading(client, admin, risk, "EvPass")
    await _record_passing_evidence(db_session_factory, strategy_id, version_id)

    resp = await client.get(f"/api/v1/strategies/{strategy_id}/go-live-evidence", headers=risk)

    body = resp.json()
    assert body["eligible"] is True
    assert body["inputs"]["num_trades"] == 31
    assert body["inputs"]["clean_shadow_mode_streak_days"] >= 10
    assert body["inputs"]["backtest_win_rate"] == 0.5
    assert body["backtest_data_source"] == "real"
    assert body["not_derived"] == []


# ---- signing off


async def test_a_strategy_with_enough_recorded_evidence_is_signed_off_with_no_numbers_typed(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev3-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev3-risk@example.com")
    strategy_id, version_id = await _strategy_in_paper_trading(client, admin, risk, "EvSign")
    await _record_passing_evidence(db_session_factory, strategy_id, version_id)

    resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility", json={}, headers=risk
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "LiveEligible"
    async with db_session_factory() as db:
        overrides = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "strategy.go_live_evidence_overridden"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert overrides == []  # nothing was overridden


async def test_without_evidence_the_sign_off_is_refused_with_the_reasons(client, make_user):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev4-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev4-risk@example.com")
    strategy_id, _ = await _strategy_in_paper_trading(client, admin, risk, "EvRefuse")

    for body in ({}, None):
        resp = await client.post(
            f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
            json=body,
            headers=risk,
        )
        assert resp.status_code == 422
        assert "only 0 trades recorded" in resp.json()["detail"]
        assert "undetermined" in resp.json()["detail"]


async def test_typed_numbers_are_an_audited_override_that_needs_a_reason(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev5-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev5-risk@example.com")
    strategy_id, _ = await _strategy_in_paper_trading(client, admin, risk, "EvOverride")

    no_reason = {k: v for k, v in OVERRIDE.items() if k != "reason"}
    refused = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
        json={"override": no_reason},
        headers=risk,
    )
    too_short = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
        json={"override": {**OVERRIDE, "reason": "ok"}},
        headers=risk,
    )
    resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
        json={"override": OVERRIDE},
        headers=risk,
    )

    assert refused.status_code == 422
    assert too_short.status_code == 422
    assert resp.status_code == 200
    assert resp.json()["status"] == "LiveEligible"
    async with db_session_factory() as db:
        entry = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "strategy.go_live_evidence_overridden"
                    )
                )
            )
            .scalars()
            .one()
        )
    assert entry.actor == "ev5-risk@example.com"
    assert entry.entity_id == strategy_id
    assert entry.details["reason"] == OVERRIDE["reason"]
    assert entry.details["supplied"]["num_trades"] == 50
    assert entry.details["derived"]["num_trades"] == 0  # what the records said
    assert entry.details["derived_eligible"] is False
    assert "num_trades" in entry.details["not_derived"]


async def test_an_override_that_does_not_pass_the_gate_is_refused_and_not_audited(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev6-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev6-risk@example.com")
    strategy_id, _ = await _strategy_in_paper_trading(client, admin, risk, "EvWeak")

    resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
        json={"override": {**OVERRIDE, "num_trades": 3}},
        headers=risk,
    )

    assert resp.status_code == 422
    async with db_session_factory() as db:
        count = len(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "strategy.go_live_evidence_overridden"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert count == 0


async def test_signing_off_an_unknown_strategy_is_a_404(client, make_user):
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ev7-risk@example.com")

    resp = await client.post(
        f"/api/v1/strategies/{uuid.uuid4()}/approve-live-eligibility", json={}, headers=risk
    )

    assert resp.status_code == 404


# ---- shadow mode over HTTP


async def test_the_shadow_runs_can_be_listed_for_one_strategy(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ev8-admin@example.com")
    mine = (
        await client.post(
            "/api/v1/strategies", json={"name": "ShMine", "objective": "o"}, headers=admin
        )
    ).json()["id"]
    other = (
        await client.post(
            "/api/v1/strategies", json={"name": "ShOther", "objective": "o"}, headers=admin
        )
    ).json()["id"]
    async with db_session_factory() as db:
        for strategy_id, outcome in ((mine, "ok"), (mine, "failed"), (other, "ok")):
            db.add(
                ShadowModeRun(
                    broker_name="zerodha",
                    has_sandbox=False,
                    confidence="local_payload_only",
                    symbol="X",
                    side="buy",
                    quantity=1,
                    order_payload={},
                    strategy_id=uuid.UUID(strategy_id),
                    outcome=outcome,
                    failure_reason="boom" if outcome == "failed" else None,
                )
            )
        await db.commit()

    resp = await client.get(f"/api/v1/shadow-mode/runs?strategy_id={mine}", headers=admin)

    runs = resp.json()
    assert len(runs) == 2
    assert {r["outcome"] for r in runs} == {"ok", "failed"}
    assert all(r["strategy_id"] == mine for r in runs)
    failed = next(r for r in runs if r["outcome"] == "failed")
    assert failed["failure_reason"] == "boom"


# ---- approvals: the people, the strategy and the reason are named


async def test_the_approvals_list_names_the_strategy_and_the_people_and_keeps_the_decision_reason(
    client, make_user
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "ap-admin@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "ap-risk@example.com")
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "ApNamed", "objective": "o"}, headers=admin
        )
    ).json()
    asked = (
        await client.post(f"/api/v1/strategies/{strategy['id']}/request-promotion", headers=admin)
    ).json()

    decided = await client.post(
        f"/api/v1/approvals/{asked['id']}/decide",
        json={"approve": False, "reason": "needs a real-data backtest first"},
        headers=risk,
    )
    listing = await client.get("/api/v1/approvals", headers=risk)

    assert decided.status_code == 200
    for row in (decided.json(), next(a for a in listing.json() if a["id"] == asked["id"])):
        assert row["subject_label"] == "ApNamed"
        assert row["requested_by_label"] == "ap-admin@example.com"
        assert row["decided_by_label"] == "ap-risk@example.com"
        assert row["decision_reason"] == "needs a real-data backtest first"
        assert row["status"] == "rejected"
