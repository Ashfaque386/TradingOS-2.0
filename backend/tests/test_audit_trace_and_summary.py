"""Phase 21 (docs/phase20-old-vs-new-comparison.md item 26): purpose-built
audit views over data `GET /audit/entries` already returns filtered, but
with no dedicated "trace one thing" or "summarize one actor" shape.
"""

from httpx import AsyncClient

from src.audit.service import write_audit_entry
from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_trade_and_actor_rows(db_session_factory) -> None:
    async with db_session_factory() as db:
        await write_audit_entry(
            db,
            actor="alice",
            action="live_order_intent.generated",
            entity_type="live_order_intent",
            entity_id="trace-1",
        )
        await write_audit_entry(
            db,
            actor="alice",
            action="live_order_intent.submitted",
            entity_type="live_order_intent",
            entity_id="trace-1",
        )
        await write_audit_entry(
            db,
            actor="alice",
            action="trade.filled",
            entity_type="trade",
            entity_id="trace-1",
        )
        # A different entity, same actor -- must not leak into trace-1's trace.
        await write_audit_entry(
            db, actor="alice", action="widget.created", entity_type="widget", entity_id="other"
        )
        await db.commit()


async def test_trade_trace_returns_rows_in_chronological_order(
    client, make_user, db_session_factory
):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin1@example.com", "supersecret1")
    await _seed_trade_and_actor_rows(db_session_factory)

    resp = await client.get("/api/v1/audit/trades/trace-1/trace", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_id"] == "trace-1"
    assert body["entry_count"] == 3
    assert [e["action"] for e in body["entries"]] == [
        "live_order_intent.generated",
        "live_order_intent.submitted",
        "trade.filled",
    ]


async def test_trade_trace_404s_for_an_entity_id_with_no_rows(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin2@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/trades/never-existed/trace", headers=_auth(token))
    assert resp.status_code == 404


async def test_trade_trace_forbidden_for_portfolio_manager(client, make_user):
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm1@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/trades/trace-1/trace", headers=_auth(token))
    assert resp.status_code == 403


async def test_actor_summary_counts_actions_and_reports_span(client, make_user, db_session_factory):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin3@example.com", "supersecret1")
    await _seed_trade_and_actor_rows(db_session_factory)

    resp = await client.get("/api/v1/audit/actors/alice/summary", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["actor"] == "alice"
    # 3 seeded actions for alice on trace-1 + 1 on the "other" widget.
    assert body["action_count"] == 4
    assert body["actions"]["live_order_intent.generated"] == 1
    assert body["actions"]["trade.filled"] == 1
    assert body["first_seen"] is not None
    assert body["last_seen"] is not None
    assert body["first_seen"] <= body["last_seen"]


async def test_actor_summary_404s_for_an_actor_with_no_rows(client, make_user):
    await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin4@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/actors/nobody@example.com/summary", headers=_auth(token))
    assert resp.status_code == 404


async def test_actor_summary_allowed_for_read_only_auditor(client, make_user, db_session_factory):
    await make_user("auditor1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor1@example.com", "supersecret1")
    await _seed_trade_and_actor_rows(db_session_factory)

    resp = await client.get("/api/v1/audit/actors/alice/summary", headers=_auth(token))
    assert resp.status_code == 200


async def test_actor_summary_forbidden_for_risk_manager(client, make_user):
    await make_user("rm1@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "rm1@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/actors/alice/summary", headers=_auth(token))
    assert resp.status_code == 403
