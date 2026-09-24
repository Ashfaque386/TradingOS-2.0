"""Admin user management (Phase 21,
docs/phase20-old-vs-new-comparison.md item 10): the only admin path to
create a user at any role, change a role, deactivate/reactivate, and
revoke sessions, plus the self-lockout guard.
"""

from sqlalchemy import select

from src.core.roles import Role
from src.models.refresh_token import RefreshToken
from src.models.user import User


async def _login(client, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_list_users_allowed_for_system_administrator(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "admin1@example.com", "supersecret1")

    resp = await client.get("/api/v1/admin/users", headers=_auth(token))
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert {"admin1@example.com", "pm1@example.com"} <= emails


async def test_list_users_forbidden_for_portfolio_manager(client, make_user):
    await make_user("pm2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm2@example.com", "supersecret1")

    resp = await client.get("/api/v1/admin/users", headers=_auth(token))
    assert resp.status_code == 403


async def test_create_user_at_any_role_directly(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin2@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": "newrisk@example.com",
            "password": "supersecret1",
            "role": "RiskManager",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "RiskManager"
    assert body["is_active"] is True

    # And the new user can actually log in.
    new_token = await _login(client, "newrisk@example.com", "supersecret1")
    assert new_token


async def test_create_user_rejects_duplicate_email(client, make_user):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin3@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/admin/users",
        json={"email": "admin3@example.com", "password": "supersecret1", "role": "ReadOnlyAuditor"},
        headers=_auth(token),
    )
    assert resp.status_code == 400


async def test_create_user_forbidden_for_read_only_auditor(client, make_user):
    await make_user("auditor1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor1@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/admin/users",
        json={"email": "x@example.com", "password": "supersecret1", "role": "ReadOnlyAuditor"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_change_role_updates_the_user(client, make_user, db_session_factory):
    admin = await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    target = await make_user("target1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "admin4@example.com", "supersecret1")
    assert admin

    resp = await client.patch(
        f"/api/v1/admin/users/{target.id}/role",
        json={"role": "RiskManager"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "RiskManager"

    async with db_session_factory() as db:
        row = await db.get(User, target.id)
        assert row.role == Role.RISK_MANAGER


async def test_change_role_404s_for_an_unknown_user(client, make_user):
    await make_user("admin5@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin5@example.com", "supersecret1")

    resp = await client.patch(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/role",
        json={"role": "RiskManager"},
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_deactivate_and_reactivate_round_trips(client, make_user):
    admin = await make_user("admin6@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    target = await make_user("target2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "admin6@example.com", "supersecret1")
    assert admin

    resp = await client.post(f"/api/v1/admin/users/{target.id}/deactivate", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # A deactivated user can no longer log in.
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "target2@example.com", "password": "supersecret1"},
    )
    assert resp.status_code == 401

    resp = await client.post(f"/api/v1/admin/users/{target.id}/reactivate", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    token2 = await _login(client, "target2@example.com", "supersecret1")
    assert token2


async def test_deactivate_refuses_to_remove_the_last_active_admin(client, make_user):
    only_admin = await make_user(
        "lastadmin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR
    )
    token = await _login(client, "lastadmin1@example.com", "supersecret1")

    resp = await client.post(
        f"/api/v1/admin/users/{only_admin.id}/deactivate", headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "last active" in resp.json()["detail"]


async def test_change_role_refuses_to_demote_the_last_active_admin(client, make_user):
    only_admin = await make_user(
        "lastadmin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR
    )
    token = await _login(client, "lastadmin2@example.com", "supersecret1")

    resp = await client.patch(
        f"/api/v1/admin/users/{only_admin.id}/role",
        json={"role": "ReadOnlyAuditor"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert "last active" in resp.json()["detail"]


async def test_deactivate_a_second_admin_is_allowed_when_another_stays_active(client, make_user):
    await make_user("admin7@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    second_admin = await make_user("admin8@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin7@example.com", "supersecret1")

    resp = await client.post(
        f"/api/v1/admin/users/{second_admin.id}/deactivate", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_revoke_sessions_invalidates_refresh_tokens(client, make_user, db_session_factory):
    admin = await make_user("admin9@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    target = await make_user("target3@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    admin_token = await _login(client, "admin9@example.com", "supersecret1")
    assert admin

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "target3@example.com", "password": "supersecret1"},
    )
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.post(
        f"/api/v1/admin/users/{target.id}/revoke-sessions", headers=_auth(admin_token)
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_count"] == 1

    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(RefreshToken).where(RefreshToken.user_id == target.id)))
            .scalars()
            .all()
        )
        assert all(row.revoked_at is not None for row in rows)

    # The now-revoked refresh token can no longer mint a new access token.
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


async def test_revoke_sessions_404s_for_an_unknown_user(client, make_user):
    await make_user("admin10@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin10@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/revoke-sessions",
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_all_admin_routes_forbidden_for_risk_manager(client, make_user):
    target = await make_user("target4@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    await make_user("rm1@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "rm1@example.com", "supersecret1")

    assert (await client.get("/api/v1/admin/users", headers=_auth(token))).status_code == 403
    assert (
        await client.post(
            "/api/v1/admin/users",
            json={"email": "z@example.com", "password": "supersecret1", "role": "ReadOnlyAuditor"},
            headers=_auth(token),
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/v1/admin/users/{target.id}/role",
            json={"role": "RiskManager"},
            headers=_auth(token),
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/admin/users/{target.id}/deactivate", headers=_auth(token))
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/admin/users/{target.id}/reactivate", headers=_auth(token))
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/admin/users/{target.id}/revoke-sessions", headers=_auth(token))
    ).status_code == 403
