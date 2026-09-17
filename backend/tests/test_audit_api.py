"""Audit API tests (Build Spec §19): list/export read-restricted to
SystemAdministrator/ReadOnlyAuditor (the explicit acceptance criterion --
neither PortfolioManager nor RiskManager may read the trail), verify
restricted to SystemAdministrator only, and CSV/NDJSON export format.

**Every login itself writes an audit entry** -- `POST /api/v1/auth/login`
is a mutating request that reaches `AuditLoggingMiddleware`
(src/observability/audit_middleware.py) like any other, so every test
below logs in *before* seeding its own fixture rows and filters list/
export queries by `entity_type=widget` to isolate the rows it actually
seeded from that unavoidable login noise, rather than asserting a bare
row count that would silently depend on middleware internals.
"""

import json

from httpx import AsyncClient

from src.audit.service import write_audit_entry
from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_widget_entries(db_session_factory) -> None:
    async with db_session_factory() as db:
        await write_audit_entry(
            db, actor="alice", action="widget.created", entity_type="widget", entity_id="1"
        )
        await write_audit_entry(
            db, actor="bob", action="widget.deleted", entity_type="widget", entity_id="2"
        )
        await db.commit()


async def test_list_entries_allowed_for_system_administrator(client, make_user, db_session_factory):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin1@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/entries", params={"entity_type": "widget"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Newest first (ordered by sequence desc).
    assert body[0]["actor"] == "bob"
    assert body[1]["actor"] == "alice"


async def test_list_entries_allowed_for_read_only_auditor(client, make_user, db_session_factory):
    await make_user("auditor1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor1@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/entries", params={"entity_type": "widget"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_entries_forbidden_for_portfolio_manager(client, make_user, db_session_factory):
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm1@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/entries", headers=_auth(token))
    assert resp.status_code == 403


async def test_list_entries_forbidden_for_risk_manager(client, make_user, db_session_factory):
    await make_user("rm1@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "rm1@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/entries", headers=_auth(token))
    assert resp.status_code == 403


async def test_list_entries_filters_by_entity_id(client, make_user, db_session_factory):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin2@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/entries", params={"entity_id": "1"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["entity_id"] == "1"


async def test_export_csv_allowed_for_system_administrator(client, make_user, db_session_factory):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin3@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/export",
        params={"format": "csv", "entity_type": "widget"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("sequence,")
    assert len(lines) == 3  # header + 2 rows


async def test_export_ndjson_allowed_for_read_only_auditor(client, make_user, db_session_factory):
    await make_user("auditor2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor2@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/export", params={"entity_type": "widget"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = resp.text.strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["actor"] == "alice"
    assert row["action"] == "widget.created"


async def test_export_forbidden_for_portfolio_manager(client, make_user, db_session_factory):
    await make_user("pm2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm2@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/export", headers=_auth(token))
    assert resp.status_code == 403


async def test_export_forbidden_for_risk_manager(client, make_user, db_session_factory):
    await make_user("rm2@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "rm2@example.com", "supersecret1")

    resp = await client.get("/api/v1/audit/export", headers=_auth(token))
    assert resp.status_code == 403


async def test_verify_allowed_for_system_administrator_and_reports_valid_chain(
    client, make_user, db_session_factory
):
    await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin4@example.com", "supersecret1")
    await _seed_widget_entries(db_session_factory)

    resp = await client.post("/api/v1/audit/verify", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_chain_valid"] is True
    assert body["diverged"] is False


async def test_verify_forbidden_for_read_only_auditor(client, make_user, db_session_factory):
    """Verify is more than a read -- a genuine divergence finding writes
    its own audit entry -- so it's SystemAdministrator-only, narrower than
    list/export's SystemAdministrator+ReadOnlyAuditor.
    """
    await make_user("auditor3@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor3@example.com", "supersecret1")

    resp = await client.post("/api/v1/audit/verify", headers=_auth(token))
    assert resp.status_code == 403


async def test_verify_forbidden_for_portfolio_manager(client, make_user, db_session_factory):
    await make_user("pm3@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm3@example.com", "supersecret1")

    resp = await client.post("/api/v1/audit/verify", headers=_auth(token))
    assert resp.status_code == 403
