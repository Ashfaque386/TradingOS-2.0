"""AuditLoggingMiddleware tests (Build Spec §19's acceptance line: "every
mutating action from every previous phase now produces a verifiable,
exportable audit entry") -- a real HTTP mutating request, through the
real ASGI app (not a direct write_audit_entry call), produces a matching
row in the hash chain.
"""

from cryptography.fernet import Fernet
from sqlalchemy import select

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.core.roles import Role
from src.main import app
from src.models.audit_log import AuditLog
from src.security.secrets_store import SecretsStore


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


async def test_middleware_action_carries_the_full_path_for_a_shared_prefix_router(
    client, make_user, db_session_factory, tmp_path
):
    """Regression for a real Phase 26 finding (docs/phase20-old-vs-new-
    comparison.md item 28's dependency-CVE pass, FastAPI 0.115->0.141):
    `src/api/routes/broker_credentials.py` and `src/api/routes/
    broker_oauth.py` are two separate APIRouters that both declare
    `prefix="/broker-credentials"` -- for exactly this shared-prefix shape,
    a nested APIRoute's own `.path` started coming back relative to its
    immediate router only, silently dropping the outer api_router's
    `/api/v1`. Confirmed live via `POST /api/v1/broker-credentials/{broker}`
    before the fix: this row's `action` was `"POST /broker-credentials/
    zerodha"`, not the real full path.
    """
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_broker_credentials_store] = lambda: store
    try:
        await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        login_resp = await client.post(
            "/api/v1/auth/login", json={"email": "admin2@example.com", "password": "supersecret1"}
        )
        token = login_resp.json()["access_token"]

        resp = await client.post(
            "/api/v1/broker-credentials/zerodha",
            json={"api_key": "k", "access_token": "t"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

        async with db_session_factory() as db:
            rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()

        # The middleware has always logged the route's path *template* for
        # a parameterized route (pre-existing, correct behavior, unrelated
        # to this fix) -- what this test actually proves is the prefix
        # portion (/api/v1/broker-credentials) is present and correct.
        assert rows[-1].action == "POST /api/v1/broker-credentials/{broker}"
    finally:
        app.dependency_overrides.pop(get_broker_credentials_store, None)
