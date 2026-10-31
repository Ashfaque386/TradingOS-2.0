"""A kill-switch reset keeps the reason its human gave.

The console asks for a reason, the request carried it, and the endpoint discarded it: the only
record of why a tripped switch was re-armed was who pressed the button. The reason is now stored
beside `reset_by`/`reset_at`, shown by `GET /kill-switch/{mode}`, and written to the audit trail
with what was being reset.
"""

from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog

RESET_AUDIT_ACTION = "kill_switch.reset"


async def _operator(client, make_user, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", Role.RISK_MANAGER)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _trip(client, headers, mode: str = "paper") -> None:
    resp = await client.post(
        f"/api/v1/kill-switch/{mode}/check",
        json={"current_equity": 80000, "peak_equity": 100000},
        headers=headers,
    )
    assert resp.json()["tripped"] is True


async def test_the_reset_reason_is_stored_and_shown(client, make_user):
    headers = await _operator(client, make_user, "ks-reason@example.com")
    await _trip(client, headers)

    reset = await client.post(
        "/api/v1/kill-switch/paper/reset",
        json={"reason": "Reviewed the fills: a bad tick, not a real loss."},
        headers=headers,
    )
    shown = await client.get("/api/v1/kill-switch/paper", headers=headers)

    assert reset.status_code == 200
    for body in (reset.json(), shown.json()):
        assert body["tripped"] is False
        assert body["reset_reason"] == "Reviewed the fills: a bad tick, not a real loss."
        assert body["reset_at"] is not None


async def test_a_trip_reports_when_it_happened(client, make_user):
    headers = await _operator(client, make_user, "ks-when@example.com")
    await _trip(client, headers)

    body = (await client.get("/api/v1/kill-switch/paper", headers=headers)).json()

    assert body["tripped"] is True
    assert body["tripped_at"] is not None
    assert body["reset_reason"] is None and body["reset_at"] is None


async def test_a_blank_reason_is_stored_as_no_reason(client, make_user):
    headers = await _operator(client, make_user, "ks-blank@example.com")
    await _trip(client, headers)

    reset = await client.post(
        "/api/v1/kill-switch/paper/reset", json={"reason": "   "}, headers=headers
    )

    assert reset.status_code == 200
    assert reset.json()["reset_reason"] is None


async def test_an_overlong_reason_is_refused_and_the_switch_stays_tripped(client, make_user):
    headers = await _operator(client, make_user, "ks-long@example.com")
    await _trip(client, headers)

    reset = await client.post(
        "/api/v1/kill-switch/paper/reset", json={"reason": "x" * 501}, headers=headers
    )

    assert reset.status_code == 422
    assert (await client.get("/api/v1/kill-switch/paper", headers=headers)).json()[
        "tripped"
    ] is True


async def test_the_audit_trail_records_the_reason_and_what_was_reset(
    client, make_user, db_session_factory
):
    headers = await _operator(client, make_user, "ks-audit@example.com")
    await _trip(client, headers)

    await client.post(
        "/api/v1/kill-switch/paper/reset", json={"reason": "cleared after review"}, headers=headers
    )

    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == RESET_AUDIT_ACTION)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor == "ks-audit@example.com"
    assert rows[0].entity_type == "kill-switch" and rows[0].entity_id == "paper"
    assert rows[0].details["reason"] == "cleared after review"
    assert "drawdown" in rows[0].details["tripped_because"].lower()
    assert rows[0].details["drawdown_pct_at_trip"] == 20.0


async def test_a_refused_reset_leaves_no_reason_row(client, make_user, db_session_factory):
    headers = await _operator(client, make_user, "ks-refused@example.com")

    resp = await client.post(
        "/api/v1/kill-switch/paper/reset", json={"reason": "nothing to reset"}, headers=headers
    )

    assert resp.status_code == 409
    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == RESET_AUDIT_ACTION)))
            .scalars()
            .all()
        )
    assert rows == []
