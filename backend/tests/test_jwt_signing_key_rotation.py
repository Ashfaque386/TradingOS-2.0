"""JWT signing-key rotation (Phase 21,
docs/phase20-old-vs-new-comparison.md item 11): the key now lives in the
`jwt_signing_keys` table, cached in-process
(src.core.security.get_current_signing_key), so a rotation takes effect
on this process's very next request with no restart -- a hard cutover,
not a grace period: every access token issued under the old key stops
validating, and every outstanding refresh token is revoked in the same
transaction.
"""

from src.core.roles import Role


async def _login(client, email: str, password: str) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_rotate_requires_system_administrator(client, make_user):
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm1@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(token["access_token"])
    )
    assert resp.status_code == 403


async def test_rotate_response_never_echoes_the_new_key(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    tokens = await _login(client, "admin1@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(tokens["access_token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"rotated_at", "rotated_by", "sessions_revoked"}
    assert body["rotated_by"] == "admin1@example.com"


async def test_access_token_issued_before_rotation_stops_working_after(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    tokens = await _login(client, "admin2@example.com", "supersecret1")
    old_access = tokens["access_token"]

    # Proven valid before rotation.
    resp = await client.get("/api/v1/auth/me", headers=_auth(old_access))
    assert resp.status_code == 200

    rotate_resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(old_access)
    )
    assert rotate_resp.status_code == 200

    resp = await client.get("/api/v1/auth/me", headers=_auth(old_access))
    assert resp.status_code == 401


async def test_login_after_rotation_issues_a_token_that_works(client, make_user):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    tokens = await _login(client, "admin3@example.com", "supersecret1")

    rotate_resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(tokens["access_token"])
    )
    assert rotate_resp.status_code == 200

    # A fresh login (real credentials, not a token) still works, and the
    # new token it issues -- under the new key -- validates.
    new_tokens = await _login(client, "admin3@example.com", "supersecret1")
    resp = await client.get("/api/v1/auth/me", headers=_auth(new_tokens["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin3@example.com"


async def test_rotation_revokes_every_outstanding_refresh_token(client, make_user):
    await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_tokens = await _login(client, "admin4@example.com", "supersecret1")

    other_tokens = await _login(client, "admin4@example.com", "supersecret1")
    other_refresh = other_tokens["refresh_token"]

    resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(admin_tokens["access_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["sessions_revoked"] >= 2  # both refresh tokens from the two logins above

    refresh_resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": other_refresh})
    assert refresh_resp.status_code == 401


async def test_rotation_writes_an_audit_entry(client, make_user, db_session_factory):
    await make_user("admin5@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    tokens = await _login(client, "admin5@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/system/jwt-signing-key/rotate", headers=_auth(tokens["access_token"])
    )
    assert resp.status_code == 200

    entries_resp = await client.get(
        "/api/v1/audit/entries",
        params={"action": "system.jwt_signing_key_rotated"},
        headers=_auth(tokens["access_token"]),
    )
    # The admin used above has SystemAdministrator, which is also a read
    # role for /audit/entries -- but the access token was just invalidated
    # by the rotation this same test triggered, so log back in first.
    if entries_resp.status_code == 401:
        fresh = await _login(client, "admin5@example.com", "supersecret1")
        entries_resp = await client.get(
            "/api/v1/audit/entries",
            params={"action": "system.jwt_signing_key_rotated"},
            headers=_auth(fresh["access_token"]),
        )
    assert entries_resp.status_code == 200
    rows = entries_resp.json()
    assert any(r["actor"] == "admin5@example.com" for r in rows)
