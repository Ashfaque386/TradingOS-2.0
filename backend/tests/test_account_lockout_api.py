"""Locking an account after wrong passwords, unlocking it, and the per-address sign-in ceiling.

The rules themselves are pinned at every boundary in test_login_lockout.py; these tests prove the
sign-in route applies them: what it stores, what it answers, what it audits, and that two wrong
passwords arriving together are both counted.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from src.core.config import get_settings
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.main import app
from src.models.audit_log import AuditLog
from src.models.user import User

EMAIL = "user@example.com"
PASSWORD = "correct-horse-1"
LIMIT = 5  # settings.login_max_failed_attempts


async def _attempt(client: AsyncClient, password: str = "wrong-password", email: str = EMAIL):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def _wrong(client: AsyncClient, times: int, email: str = EMAIL):
    return [await _attempt(client, email=email) for _ in range(times)]


async def _row(db_session_factory, email: str = EMAIL) -> User:
    async with db_session_factory() as db:
        return (await db.execute(select(User).where(User.email == email))).scalar_one()


async def _set(db_session_factory, email: str = EMAIL, **fields) -> None:
    async with db_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        for name, value in fields.items():
            setattr(user, name, value)
        await db.commit()


@pytest_asyncio.fixture
async def account(make_user):
    return await make_user(EMAIL, PASSWORD, Role.PORTFOLIO_MANAGER)


# ---- the lock ----


async def test_wrong_passwords_below_the_limit_are_refused_and_counted(
    client, account, db_session_factory
):
    responses = await _wrong(client, LIMIT - 1)

    assert [r.status_code for r in responses] == [401] * (LIMIT - 1)
    row = await _row(db_session_factory)
    assert row.failed_login_attempts == LIMIT - 1
    assert row.locked_until is None


async def test_the_wrong_password_that_reaches_the_limit_locks_the_account_and_says_so(
    client, account, db_session_factory
):
    await _wrong(client, LIMIT - 1)

    resp = await _attempt(client)

    assert resp.status_code == 423
    assert "locked" in resp.json()["detail"].lower()
    assert "15 minutes" in resp.json()["detail"]
    assert 14 * 60 < int(resp.headers["Retry-After"]) <= 15 * 60
    row = await _row(db_session_factory)
    assert row.locked_until is not None
    assert row.locked_until > datetime.now(UTC) + timedelta(minutes=14)
    assert row.failed_login_attempts == 0


async def test_a_locked_account_refuses_even_the_correct_password(client, account):
    await _wrong(client, LIMIT)

    resp = await _attempt(client, password=PASSWORD)

    assert resp.status_code == 423
    assert not resp.cookies
    assert "access_token" not in resp.json()


async def test_a_lock_is_audited_with_who_it_was_and_where_from(
    client, account, db_session_factory
):
    await _wrong(client, LIMIT)

    async with db_session_factory() as db:
        entry = (
            await db.execute(select(AuditLog).where(AuditLog.action == "auth.account_locked"))
        ).scalar_one()
    assert entry.actor == EMAIL
    assert entry.entity_type == "user"
    assert entry.details["client_ip"] == "127.0.0.1"
    assert entry.details["locked_until"]


async def test_only_the_failure_that_locks_writes_the_audit_entry(
    client, account, db_session_factory
):
    await _wrong(client, LIMIT - 1)
    async with db_session_factory() as db:
        before = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "auth.account_locked")))
            .scalars()
            .all()
        )
    assert before == []

    await _wrong(client, 3)  # the fifth locks, the next two are refused without being counted

    async with db_session_factory() as db:
        after = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "auth.account_locked")))
            .scalars()
            .all()
        )
    assert len(after) == 1


async def test_a_lock_that_has_run_out_lets_the_correct_password_in_and_clears_the_record(
    client, account, db_session_factory
):
    await _wrong(client, LIMIT)
    await _set(db_session_factory, locked_until=datetime.now(UTC) - timedelta(seconds=1))

    resp = await _attempt(client, password=PASSWORD)

    assert resp.status_code == 200
    row = await _row(db_session_factory)
    assert (row.failed_login_attempts, row.last_failed_login_at, row.locked_until) == (
        0,
        None,
        None,
    )


async def test_a_correct_password_forgets_the_wrong_ones_before_it(
    client, account, db_session_factory
):
    await _wrong(client, LIMIT - 1)
    assert (await _attempt(client, password=PASSWORD)).status_code == 200
    assert (await _row(db_session_factory)).failed_login_attempts == 0

    responses = await _wrong(client, LIMIT - 1)

    assert [r.status_code for r in responses] == [401] * (
        LIMIT - 1
    )  # a fresh run, still not locked


async def test_old_failures_age_out_of_the_window(client, account, db_session_factory):
    await _wrong(client, LIMIT - 1)
    await _set(db_session_factory, last_failed_login_at=datetime.now(UTC) - timedelta(minutes=16))

    resp = await _attempt(client)

    assert resp.status_code == 401
    assert (await _row(db_session_factory)).failed_login_attempts == 1


async def test_a_lock_on_one_account_does_not_touch_another(client, account, make_user):
    await make_user("other@example.com", PASSWORD, Role.RISK_MANAGER)
    await _wrong(client, LIMIT)

    assert (await _attempt(client, password=PASSWORD, email="other@example.com")).status_code == 200


async def test_guesses_at_an_email_nobody_has_are_refused_forever_but_never_lock_anything(client):
    responses = await _wrong(client, LIMIT + 3, email="nobody@example.com")

    assert {r.status_code for r in responses} == {401}


async def test_the_correct_password_of_an_inactive_account_is_refused_without_counting(
    client, account, db_session_factory
):
    await _set(db_session_factory, is_active=False)

    resp = await _attempt(client, password=PASSWORD)

    assert resp.status_code == 401
    assert (await _row(db_session_factory)).failed_login_attempts == 0


async def test_wrong_passwords_arriving_together_are_all_counted(
    client, account, db_session_factory
):
    responses = await asyncio.gather(*(_attempt(client) for _ in range(LIMIT)))

    assert sorted(r.status_code for r in responses) == [401] * (LIMIT - 1) + [423]
    assert (await _row(db_session_factory)).locked_until is not None


async def test_the_limits_are_settings(client, account, db_session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_failed_attempts", 2)
    monkeypatch.setattr(get_settings(), "login_lockout_minutes", 3)

    assert (await _attempt(client)).status_code == 401
    resp = await _attempt(client)

    assert resp.status_code == 423
    assert "3 minutes" in resp.json()["detail"]
    assert int(resp.headers["Retry-After"]) <= 3 * 60


# ---- an administrator unlocks ----


async def _admin_headers(client: AsyncClient, make_user) -> dict:
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login = await _attempt(client, password="supersecret1", email="admin@example.com")
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_an_administrator_can_unlock_a_locked_account(client, account, make_user):
    headers = await _admin_headers(client, make_user)
    await _wrong(client, LIMIT)
    assert (await _attempt(client, password=PASSWORD)).status_code == 423

    resp = await client.post(f"/api/v1/admin/users/{account.id}/unlock", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["locked_until"] is None
    assert (await _attempt(client, password=PASSWORD)).status_code == 200


async def test_the_user_list_shows_who_is_locked(client, account, make_user):
    headers = await _admin_headers(client, make_user)
    await _wrong(client, LIMIT)

    users = (await client.get("/api/v1/admin/users", headers=headers)).json()

    locked = {u["email"]: u["locked_until"] for u in users}
    assert locked[EMAIL] is not None
    assert locked["admin@example.com"] is None


async def test_unlocking_an_account_that_is_not_locked_is_refused_and_not_audited(
    client, account, make_user, db_session_factory
):
    headers = await _admin_headers(client, make_user)

    resp = await client.post(f"/api/v1/admin/users/{account.id}/unlock", headers=headers)

    assert resp.status_code == 409
    async with db_session_factory() as db:
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert not any(a.endswith("/unlock") for a in actions)


async def test_unlocking_an_unknown_account_is_404(client, make_user):
    headers = await _admin_headers(client, make_user)

    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-4000-8000-000000000000/unlock", headers=headers
    )

    assert resp.status_code == 404


async def test_only_an_administrator_can_unlock(client, account, make_user):
    await make_user("risk@example.com", "supersecret1", Role.RISK_MANAGER)
    login = await _attempt(client, password="supersecret1", email="risk@example.com")
    await _wrong(client, LIMIT)

    resp = await client.post(
        f"/api/v1/admin/users/{account.id}/unlock",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert resp.status_code == 403


# ---- the per-address ceiling on sign-in attempts ----


@pytest_asyncio.fixture
async def limited(redis_client, monkeypatch):
    """The real per-address limit (3 a minute), against real Redis, with the clock held still so a
    minute boundary cannot fall between the attempts."""
    keys = [k async for k in redis_client.scan_iter("auth:login-rate:*")]
    if keys:
        await redis_client.delete(*keys)
    app.state.login_rate_limit_per_minute = 3
    app.dependency_overrides[get_redis] = lambda: redis_client
    monkeypatch.setattr("src.core.rate_limit.time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    yield
    app.state.login_rate_limit_per_minute = 10**9
    app.dependency_overrides.pop(get_redis, None)
    keys = [k async for k in redis_client.scan_iter("auth:login-rate:*")]
    if keys:
        await redis_client.delete(*keys)


async def test_one_address_trying_again_and_again_is_slowed_whoever_the_accounts_are(
    client, limited
):
    responses = [await _attempt(client, email=f"nobody{n}@example.com") for n in range(5)]

    assert [r.status_code for r in responses] == [401, 401, 401, 429, 429]
    assert responses[3].headers["Retry-After"] == "60"


async def test_a_header_the_client_sends_does_not_buy_a_fresh_allowance(client, limited):
    responses = [
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever1"},
            headers={"X-Forwarded-For": f"203.0.113.{n}"},
        )
        for n in range(5)
    ]

    assert [r.status_code for r in responses] == [401, 401, 401, 429, 429]


async def test_behind_a_trusted_proxy_each_forwarded_address_has_its_own_allowance(
    client, limited, monkeypatch
):
    monkeypatch.setattr(get_settings(), "trusted_proxies", "127.0.0.1")

    responses = [
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever1"},
            headers={"X-Forwarded-For": f"203.0.113.{n}"},
        )
        for n in range(6)
    ]

    assert {r.status_code for r in responses} == {401}


async def test_an_unreachable_redis_does_not_stop_anyone_signing_in(client, account):
    class Broken:
        async def incr(self, *_a, **_k):
            raise ConnectionError("redis is down")

    app.dependency_overrides[get_redis] = lambda: Broken()
    try:
        resp = await _attempt(client, password=PASSWORD)
    finally:
        app.dependency_overrides.pop(get_redis, None)

    assert resp.status_code == 200
