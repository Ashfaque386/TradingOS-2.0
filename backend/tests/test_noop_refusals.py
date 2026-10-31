"""The refusals the no-op audit sweep classified as already correct, pinned.

The sweep found these transitions already answer 404/409 when their target state
holds, but none of them had a test that said so. Each test here does the transition
once, repeats it, and checks both the status and that the audit trail holds exactly
one row -- the real one.
"""

from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog


async def _login(client, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _rows(db_session_factory, action: str) -> list[AuditLog]:
    async with db_session_factory() as db:
        query = select(AuditLog).where(AuditLog.action == action)
        return list((await db.execute(query)).scalars().all())


APPROVAL_DECIDE = "POST /api/v1/approvals/{approval_id}/decide"
PROMOTE = "POST /api/v1/strategies/{strategy_id}/promote"
APPROVE_LIVE = "POST /api/v1/strategies/{strategy_id}/approve-live-eligibility"
RL_CONFIRM = "POST /api/v1/risk-limits/{change_id}/confirm"
RL_APPLY = "POST /api/v1/risk-limits/{change_id}/apply"
RL_REJECT = "POST /api/v1/risk-limits/{change_id}/reject"
CHAT_DELETE = "DELETE /api/v1/chat/sessions/{session_id}"


async def test_deciding_an_approval_twice_is_refused_and_audited_once(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-nr1@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "risk-nr1@example.com")
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "NR1", "objective": "o"}, headers=admin
        )
    ).json()
    approval = (
        await client.post(f"/api/v1/strategies/{strategy['id']}/request-promotion", headers=admin)
    ).json()
    url = f"/api/v1/approvals/{approval['id']}/decide"

    first = await client.post(url, json={"approve": True}, headers=risk)
    again = await client.post(url, json={"approve": False}, headers=risk)

    assert first.status_code == 200 and first.json()["status"] == "approved"
    assert again.status_code == 404 and "already decided" in again.json()["detail"]
    assert len(await _rows(db_session_factory, APPROVAL_DECIDE)) == 1


async def test_promoting_a_strategy_twice_is_refused_and_audited_once(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-nr2@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "risk-nr2@example.com")
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "NR2", "objective": "o"}, headers=admin
        )
    ).json()
    approval = (
        await client.post(f"/api/v1/strategies/{strategy['id']}/request-promotion", headers=admin)
    ).json()
    await client.post(
        f"/api/v1/approvals/{approval['id']}/decide", json={"approve": True}, headers=risk
    )
    url = f"/api/v1/strategies/{strategy['id']}/promote"

    first = await client.post(url, headers=admin)
    again = await client.post(url, headers=admin)

    assert first.status_code == 200
    assert again.status_code == 409
    assert len(await _rows(db_session_factory, PROMOTE)) == 1


async def test_approving_live_eligibility_for_a_strategy_not_in_paper_trading_is_refused(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-nr3@example.com")
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "NR3", "objective": "o"}, headers=admin
        )
    ).json()
    body = {
        "num_trades": 30,
        "calendar_days_running": 21,
        "clean_shadow_mode_streak_days": 10,
        "live_win_rate": 0.55,
        "backtest_win_rate": 0.5,
    }

    resp = await client.post(
        f"/api/v1/strategies/{strategy['id']}/approve-live-eligibility", json=body, headers=admin
    )

    assert resp.status_code == 409
    assert await _rows(db_session_factory, APPROVE_LIVE) == []


async def test_rejecting_a_risk_limit_change_twice_is_refused_and_audited_once(
    client, make_user, db_session_factory
):
    risk = await _login(client, make_user, Role.RISK_MANAGER, "risk-nr4b@example.com")
    change = (
        await client.post(
            "/api/v1/risk-limits/stage",
            json={"limit_name": "max_drawdown_pct", "proposed_value": 9.0},
            headers=risk,
        )
    ).json()
    url = f"/api/v1/risk-limits/{change['id']}/reject"

    rejected = await client.post(url, json={"reason": "no"}, headers=risk)
    rejected_again = await client.post(url, json={"reason": "no"}, headers=risk)

    assert rejected.status_code == 200
    assert rejected_again.status_code == 409
    assert len(await _rows(db_session_factory, RL_REJECT)) == 1
    assert len(await _rows(db_session_factory, "risk_limit.change_rejected")) == 1


async def test_confirming_or_applying_a_risk_limit_change_twice_is_refused_and_audited_once(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-nr4@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "risk-nr4@example.com")
    change = (
        await client.post(
            "/api/v1/risk-limits/stage",
            json={"limit_name": "max_drawdown_pct", "proposed_value": 9.0},
            headers=admin,
        )
    ).json()
    base = f"/api/v1/risk-limits/{change['id']}"

    confirmed = await client.post(f"{base}/confirm", headers=risk)
    confirmed_again = await client.post(f"{base}/confirm", headers=risk)
    applied = await client.post(f"{base}/apply", headers=risk)
    applied_again = await client.post(f"{base}/apply", headers=risk)

    assert confirmed.status_code == 200 and applied.status_code == 200
    assert confirmed_again.status_code == 409
    assert applied_again.status_code == 409
    assert len(await _rows(db_session_factory, RL_CONFIRM)) == 1
    assert len(await _rows(db_session_factory, RL_APPLY)) == 1


async def test_deleting_a_chat_session_twice_is_a_404_and_audited_once(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-nr5@example.com")
    session = (
        await client.post("/api/v1/chat/sessions", json={"title": "to delete"}, headers=headers)
    ).json()
    url = f"/api/v1/chat/sessions/{session['id']}"

    first = await client.delete(url, headers=headers)
    again = await client.delete(url, headers=headers)

    assert first.status_code == 204 and again.status_code == 404
    assert len(await _rows(db_session_factory, CHAT_DELETE)) == 1
