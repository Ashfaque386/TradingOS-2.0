"""AuditLoggingMiddleware tests (Build Spec §19's acceptance line: "every
mutating action from every previous phase now produces a verifiable,
exportable audit entry") -- a real HTTP mutating request, through the
real ASGI app (not a direct write_audit_entry call), produces a matching
row in the hash chain.
"""

from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog


async def test_post_register_produces_an_audit_entry(client, db_session_factory):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "newuser@example.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.action == "POST /api/v1/auth/register"
    assert row.entity_type == "auth"
    assert row.sequence == 1


async def test_a_get_request_produces_no_audit_entry(client, db_session_factory):
    resp = await client.get("/health")
    assert resp.status_code == 200

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()
    assert rows == []


async def test_a_failed_mutating_request_produces_no_audit_entry(client, db_session_factory):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong"}
    )
    assert resp.status_code >= 400

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()
    assert rows == []


async def test_middleware_entry_carries_the_authenticated_actor(
    client, make_user, db_session_factory
):
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "supersecret1"}
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    resp = await client.post("/api/v1/audit/verify", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()

    # Row 1: the login itself (unauthenticated -> "anonymous"). Row 2: the
    # verify call, made with a real Bearer token -- the middleware's actor
    # extraction reads it off request.state.current_user (set by
    # src.api.deps.get_current_user), proving it isn't hardcoded to
    # "anonymous".
    assert rows[0].actor == "anonymous"
    assert rows[0].action == "POST /api/v1/auth/login"
    assert rows[1].actor == "admin@example.com"
    assert rows[1].action == "POST /api/v1/audit/verify"
