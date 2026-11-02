"""Registration, sign-in and the refresh-token cookie.

The refresh token never appears in a response body: it is an HttpOnly cookie, so these tests read it
from the client's cookie jar (or from `Set-Cookie`), exactly as a browser keeps it.
"""

import asyncio

from httpx import AsyncClient
from sqlalchemy import func, select

from src.core.config import get_settings
from src.core.roles import Role
from src.core.web_session import REFRESH_COOKIE_NAME
from src.models.audit_log import AuditLog
from src.models.refresh_token import RefreshToken
from src.models.user import User


async def _register(
    client: AsyncClient,
    email: str = "first@example.com",
    password: str = "supersecret1",
):
    return await client.post("/api/v1/auth/register", json={"email": email, "password": password})


async def _login(
    client: AsyncClient, email: str = "user@example.com", password: str = "correct-horse-1"
):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _refresh_cookie(resp) -> str | None:
    return resp.cookies.get(REFRESH_COOKIE_NAME)


def _set_cookie_header(resp) -> str:
    return next(
        v
        for k, v in resp.headers.multi_items()
        if k.lower() == "set-cookie" and REFRESH_COOKIE_NAME in v
    )


async def _signed_in(
    client: AsyncClient, make_user, email="user@example.com", password="correct-horse-1"
):
    await make_user(email, password, Role.PORTFOLIO_MANAGER)
    resp = await _login(client, email, password)
    assert resp.status_code == 200
    return resp


# ---- registration is for the first account only ----


async def test_register_first_user_becomes_system_administrator(client: AsyncClient):
    resp = await _register(client)

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "first@example.com"
    assert body["role"] == "SystemAdministrator"
    assert body["is_active"] is True
    assert "hashed_password" not in body


async def test_registration_is_closed_once_an_account_exists(
    client: AsyncClient, db_session_factory
):
    await _register(client, email="first@example.com")

    resp = await _register(client, email="second@example.com")

    assert resp.status_code == 403
    assert "Registration is closed" in resp.json()["detail"]
    async with db_session_factory() as db:
        assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1


async def test_closed_registration_answers_the_same_for_an_email_that_has_an_account(
    client: AsyncClient,
):
    """It must not say which addresses have accounts."""
    await _register(client, email="first@example.com")

    existing = await _register(client, email="first@example.com")
    unknown = await _register(client, email="nobody@example.com")

    assert existing.status_code == unknown.status_code == 403
    assert existing.json() == unknown.json()


async def test_a_client_cannot_choose_the_role_of_the_first_account(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "sneaky@example.com", "password": "supersecret1", "role": "ReadOnlyAuditor"},
    )

    assert resp.status_code == 201
    assert resp.json()["role"] == "SystemAdministrator"


async def test_two_registrations_arriving_together_on_an_empty_system_make_one_administrator(
    client: AsyncClient, db_session_factory
):
    results = await asyncio.gather(
        _register(client, email="a@example.com"),
        _register(client, email="b@example.com"),
        _register(client, email="c@example.com"),
    )

    assert sorted(r.status_code for r in results) == [201, 403, 403]
    async with db_session_factory() as db:
        users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1
    assert users[0].role == Role.SYSTEM_ADMINISTRATOR


async def test_registration_status_is_open_on_an_empty_system_and_closed_after(client: AsyncClient):
    before = (await client.get("/api/v1/auth/registration-status")).json()
    await _register(client)
    after = (await client.get("/api/v1/auth/registration-status")).json()

    assert before["open"] is True
    assert "first account" in before["detail"]
    assert after["open"] is False
    assert "administrator creates accounts" in after["detail"]


async def test_registration_status_needs_no_sign_in(client: AsyncClient):
    resp = await client.get("/api/v1/auth/registration-status")

    assert resp.status_code == 200


# ---- sign-in ----


async def test_login_returns_an_access_token_and_sets_the_refresh_token_as_a_cookie(
    client: AsyncClient, make_user
):
    resp = await _signed_in(client, make_user)

    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert 0 < body["expires_in"] <= 15 * 60
    assert "refresh_token" not in body
    assert _refresh_cookie(resp)


async def test_the_refresh_cookie_is_httponly_scoped_and_samesite_strict(
    client: AsyncClient, make_user
):
    header = _set_cookie_header(await _signed_in(client, make_user)).lower()

    assert "httponly" in header
    assert "path=/api/v1/auth" in header
    assert "samesite=strict" in header
    assert "max-age=604800" in header  # seven days
    assert "secure" not in header.replace("httponly", "")  # plain http: not marked Secure


async def test_the_refresh_cookie_is_secure_when_a_trusted_proxy_says_the_client_used_https(
    client: AsyncClient, make_user, monkeypatch
):
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "trusted_proxies", "127.0.0.1")
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "correct-horse-1"},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert "secure" in _set_cookie_header(resp).lower().replace("httponly", "")


async def test_the_refresh_cookie_can_be_forced_secure_or_not(
    client: AsyncClient, make_user, monkeypatch
):
    from src.core.config import get_settings

    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", "true")
    forced = await _login(client, "user@example.com")
    assert "secure" in _set_cookie_header(forced).lower().replace("httponly", "")

    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", "false")
    monkeypatch.setattr(get_settings(), "refresh_cookie_samesite", "lax")
    relaxed = await _login(client, "user@example.com")
    header = _set_cookie_header(relaxed).lower()
    assert "secure" not in header.replace("httponly", "")
    assert "samesite=lax" in header


async def test_samesite_none_is_always_secure_because_browsers_refuse_it_otherwise(
    client: AsyncClient, make_user, monkeypatch
):
    from src.core.config import get_settings

    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    monkeypatch.setattr(get_settings(), "refresh_cookie_samesite", "none")
    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", "false")

    header = _set_cookie_header(await _login(client, "user@example.com")).lower()

    assert "samesite=none" in header
    assert "secure" in header.replace("httponly", "")


async def test_sign_in_responses_are_not_cacheable(client: AsyncClient, make_user):
    resp = await _signed_in(client, make_user)

    assert resp.headers["cache-control"] == "no-store"


async def test_login_wrong_password_rejected(client: AsyncClient, make_user):
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)

    resp = await _login(client, password="wrong-password")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"
    assert _refresh_cookie(resp) is None


async def test_login_unknown_email_gets_the_same_answer_as_a_wrong_password(
    client: AsyncClient, make_user
):
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)

    wrong = await _login(client, password="wrong-password")
    unknown = await _login(client, email="nobody@example.com", password="whatever1")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_an_unknown_email_still_pays_for_a_password_check(
    client: AsyncClient, make_user, monkeypatch
):
    """Otherwise it is answered in microseconds and a real account in tens of milliseconds, which
    tells a caller which addresses have accounts."""
    from src.api.routes import auth as auth_routes

    burns: list[str] = []

    async def spy(password: str) -> None:
        burns.append(password)

    monkeypatch.setattr(auth_routes, "burn_password_check_time", spy)
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)

    await _login(client, email="nobody@example.com", password="guess-one")
    await _login(client, password="wrong-password")  # a known account verifies for real instead

    assert burns == ["guess-one"]


async def test_password_hashing_does_not_block_the_event_loop(client: AsyncClient, make_user):
    """A sign-in runs argon2 for tens of milliseconds; a tick of the loop must still get through."""
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    task = asyncio.create_task(heartbeat())
    try:
        await asyncio.gather(*(_login(client, password="wrong-password") for _ in range(3)))
    finally:
        task.cancel()

    assert ticks >= 10


async def test_an_inactive_account_cannot_sign_in_and_is_not_told_why(
    client: AsyncClient, make_user, db_session_factory
):
    user = await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    async with db_session_factory() as db:
        row = await db.get(User, user.id)
        row.is_active = False
        await db.commit()

    resp = await _login(client)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_me_requires_bearer_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")

    assert resp.status_code in (401, 403)


async def test_me_returns_current_user(client: AsyncClient, make_user):
    login = await _signed_in(client, make_user)

    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )

    assert resp.status_code == 200
    assert resp.json()["email"] == "user@example.com"


# ---- refresh ----


async def test_refresh_rotates_the_cookie_and_returns_a_new_access_token(
    client: AsyncClient, make_user
):
    login = await _signed_in(client, make_user)
    first_cookie = _refresh_cookie(login)

    resp = await client.post("/api/v1/auth/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] != login.json()["access_token"]
    assert "refresh_token" not in body
    assert _refresh_cookie(resp) and _refresh_cookie(resp) != first_cookie


async def test_the_new_access_token_works(client: AsyncClient, make_user):
    await _signed_in(client, make_user)

    refreshed = await client.post("/api/v1/auth/refresh")
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    )

    assert me.status_code == 200


async def test_refresh_reuse_detection_revokes_the_family_and_is_audited(
    client: AsyncClient, make_user, db_session_factory, monkeypatch
):
    # With the allowance for a lost reply off (tests/test_refresh_grace.py has it on): a retired
    # token coming back at once is a replay.
    monkeypatch.setattr(get_settings(), "refresh_token_reuse_grace_seconds", 0)
    login = await _signed_in(client, make_user)
    first_cookie = _refresh_cookie(login)
    rotated = await client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200
    second_cookie = _refresh_cookie(rotated)

    # A copy of the already-redeemed token is replayed (by whoever took it).
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, first_cookie, path="/api/v1/auth")
    replay = await client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401

    # The legitimately rotated token is now dead too: the whole family was revoked.
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, second_cookie, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401

    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "auth.refresh_token_reuse_detected")
            )
        ).scalar_one()
    assert entry.details["family_id"]


async def test_a_refresh_the_server_refuses_clears_the_dead_cookie(client: AsyncClient, make_user):
    await _signed_in(client, make_user)
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, "not-a-real-token", path="/api/v1/auth")

    resp = await client.post("/api/v1/auth/refresh")

    assert resp.status_code == 401
    cleared = _set_cookie_header(resp).lower()
    assert 'tradingos_refresh=""' in cleared or "max-age=0" in cleared
    assert "path=/api/v1/auth" in cleared


async def test_refresh_without_a_cookie_is_401(client: AsyncClient):
    resp = await client.post("/api/v1/auth/refresh")

    assert resp.status_code == 401


async def test_an_access_token_is_not_accepted_as_a_refresh_token(client: AsyncClient, make_user):
    login = await _signed_in(client, make_user)
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, login.json()["access_token"], path="/api/v1/auth")

    resp = await client.post("/api/v1/auth/refresh")

    assert resp.status_code == 401


async def test_refresh_of_a_deactivated_account_is_refused(
    client: AsyncClient, make_user, db_session_factory
):
    login_user = await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    assert (await _login(client)).status_code == 200
    async with db_session_factory() as db:
        row = await db.get(User, login_user.id)
        row.is_active = False
        await db.commit()

    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_refresh_is_not_written_to_the_audit_trail_but_sign_in_is(
    client: AsyncClient, make_user, db_session_factory
):
    await _signed_in(client, make_user)
    for _ in range(3):
        assert (await client.post("/api/v1/auth/refresh")).status_code == 200

    async with db_session_factory() as db:
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("POST /api/v1/auth/login") == 1
    assert "POST /api/v1/auth/refresh" not in actions


# ---- the browser check on the two endpoints that use the cookie ----


async def test_refresh_from_another_sites_page_is_refused(client: AsyncClient, make_user):
    await _signed_in(client, make_user)

    resp = await client.post("/api/v1/auth/refresh", headers={"Origin": "https://evil.example"})

    assert resp.status_code == 403


async def test_refresh_from_the_console_origin_is_accepted(client: AsyncClient, make_user):
    await _signed_in(client, make_user)

    resp = await client.post("/api/v1/auth/refresh", headers={"Origin": "http://localhost:3000"})

    assert resp.status_code == 200


async def test_refresh_from_the_very_host_that_was_addressed_is_accepted(
    client: AsyncClient, make_user
):
    """The console and the API served from one address (the all-in-one image)."""
    await _signed_in(client, make_user)

    resp = await client.post("/api/v1/auth/refresh", headers={"Origin": "http://test"})

    assert resp.status_code == 200


async def test_a_foreign_origin_cannot_sign_the_user_out_either(client: AsyncClient, make_user):
    await _signed_in(client, make_user)

    resp = await client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})

    assert resp.status_code == 403
    assert (await client.post("/api/v1/auth/refresh")).status_code == 200  # still signed in


# ---- logout ----


async def test_logout_revokes_the_refresh_token_and_clears_the_cookie(
    client: AsyncClient, make_user, db_session_factory
):
    login = await _signed_in(client, make_user)
    cookie = _refresh_cookie(login)

    logout = await client.post("/api/v1/auth/logout")

    assert logout.status_code == 204
    assert "max-age=0" in _set_cookie_header(logout).lower()
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, cookie, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    async with db_session_factory() as db:
        rows = (await db.execute(select(RefreshToken))).scalars().all()
    assert rows and all(row.revoked_at is not None for row in rows)


async def test_logout_without_a_session_is_still_fine(client: AsyncClient):
    resp = await client.post("/api/v1/auth/logout")

    assert resp.status_code == 204


async def test_the_sign_in_policy_is_shown_to_a_signed_in_user_only(client: AsyncClient, make_user):
    assert (await client.get("/api/v1/auth/sign-in-policy")).status_code in (401, 403)
    login = await _signed_in(client, make_user)

    resp = await client.get(
        "/api/v1/auth/sign-in-policy",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "max_failed_attempts": 5,
        "failure_window_minutes": 15,
        "lockout_minutes": 15,
        "access_token_minutes": 15,
        "session_days": 7,
    }
