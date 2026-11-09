"""A refresh whose reply never reached the browser must not end the session.

Each refresh retires the cookie it was given and answers with a new one. A reload can cancel the
request after the server has answered: the new cookie is never stored, and the page presents the old
one again. Within a few seconds, and while the token that replaced it has never been used, that is
answered with a fresh token. After that, or once the replacement was used, or once the replacement
was ended by a sign-out, a retired token coming back is a replay and ends the session as ever.
"""

import asyncio
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select, update

from src.api.routes import auth as auth_routes
from src.core.config import get_settings
from src.core.roles import Role
from src.core.web_session import REFRESH_COOKIE_NAME
from src.models.audit_log import AuditLog
from src.models.refresh_token import RefreshToken
from src.models.user import User

REFRESH = "/api/v1/auth/refresh"


async def _signed_in(client: AsyncClient, make_user) -> str:
    await make_user("user@example.com", "correct-horse-1", Role.PORTFOLIO_MANAGER)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    assert resp.status_code == 200
    return resp.cookies.get(REFRESH_COOKIE_NAME)


async def _present(client: AsyncClient, cookie: str):
    """Sends the refresh with exactly this cookie, as a browser holding it would."""
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, cookie, path="/api/v1/auth")
    return await client.post(REFRESH)


def _cookie(resp) -> str:
    return resp.cookies.get(REFRESH_COOKIE_NAME)


async def _actions(db_session_factory, action: str) -> list[AuditLog]:
    async with db_session_factory() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# ---- the reply was lost ----


async def test_a_retired_token_coming_back_just_after_gets_a_fresh_one(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    lost = await _present(client, first)  # the server answered; the browser never stored this
    assert lost.status_code == 200

    retry = await _present(client, first)

    assert retry.status_code == 200
    assert retry.json()["access_token"]
    assert _cookie(retry) not in (first, _cookie(lost))
    # and the session goes on from the cookie the browser did get
    assert (await _present(client, _cookie(retry))).status_code == 200
    assert not await _actions(db_session_factory, "auth.refresh_token_reuse_detected")


async def test_the_retry_is_recorded_once_with_how_long_after(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    await _present(client, first)
    await _present(client, first)

    [entry] = await _actions(db_session_factory, "auth.refresh_token_retried")

    assert entry.details["family_id"]
    assert (
        0
        <= entry.details["seconds_since_rotation"]
        < get_settings().refresh_token_reuse_grace_seconds
    )
    assert "client_ip" in entry.details


async def test_the_token_that_was_thought_lost_still_works_if_the_browser_did_get_it(
    client: AsyncClient, make_user
):
    first = await _signed_in(client, make_user)
    maybe_lost = _cookie(await _present(client, first))
    await _present(client, first)

    assert (await _present(client, maybe_lost)).status_code == 200


async def test_two_copies_of_one_token_arriving_together_both_get_a_session(
    client: AsyncClient, make_user
):
    first = await _signed_in(client, make_user)
    client.cookies.clear()
    headers = {"Cookie": f"{REFRESH_COOKIE_NAME}={first}"}

    answers = await asyncio.gather(
        client.post(REFRESH, headers=headers), client.post(REFRESH, headers=headers)
    )

    assert sorted(a.status_code for a in answers) == [200, 200]
    assert _cookie(answers[0]) != _cookie(answers[1])


async def test_a_copy_waiting_on_the_lock_never_sees_the_old_token_retired_without_its_replacement(
    client: AsyncClient, make_user, db_session_factory, monkeypatch
):
    """Retiring the old token and storing its replacement are one transaction, so the row lock is
    held until the replacement exists. Issuing is slowed here to hold that moment open: when the
    two were committed separately, the waiting copy got the lock in between, found no replacement,
    took itself for a stolen token and ended the session."""
    first = await _signed_in(client, make_user)
    issue = auth_routes.create_access_token

    async def slow_issue(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await issue(*args, **kwargs)

    monkeypatch.setattr(auth_routes, "create_access_token", slow_issue)
    client.cookies.clear()
    headers = {"Cookie": f"{REFRESH_COOKIE_NAME}={first}"}

    answers = await asyncio.gather(
        client.post(REFRESH, headers=headers), client.post(REFRESH, headers=headers)
    )

    assert sorted(a.status_code for a in answers) == [200, 200]
    assert not await _actions(db_session_factory, "auth.refresh_token_reuse_detected")
    assert len(await _actions(db_session_factory, "auth.refresh_token_retried")) == 1


# ---- where it stops ----


async def test_once_the_replacement_has_been_used_the_old_token_is_a_replay(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    second = _cookie(await _present(client, first))
    third = _cookie(await _present(client, second))  # the browser moved on

    replay = await _present(client, first)

    assert replay.status_code == 401
    assert (await _present(client, third)).status_code == 401  # the family is gone
    assert await _actions(db_session_factory, "auth.refresh_token_reuse_detected")
    assert not await _actions(db_session_factory, "auth.refresh_token_retried")


async def test_after_the_window_a_retired_token_is_a_replay(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    await _present(client, first)
    long_ago = timedelta(seconds=get_settings().refresh_token_reuse_grace_seconds + 1)
    async with db_session_factory() as db:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.used_at.is_not(None))
            .values(used_at=RefreshToken.used_at - long_ago)
        )
        await db.commit()

    replay = await _present(client, first)

    assert replay.status_code == 401
    assert await _actions(db_session_factory, "auth.refresh_token_reuse_detected")
    assert not await _actions(db_session_factory, "auth.refresh_token_retried")


async def test_a_grace_of_zero_turns_it_off(
    client: AsyncClient, make_user, db_session_factory, monkeypatch
):
    monkeypatch.setattr(get_settings(), "refresh_token_reuse_grace_seconds", 0)
    first = await _signed_in(client, make_user)
    await _present(client, first)

    replay = await _present(client, first)

    assert replay.status_code == 401
    assert await _actions(db_session_factory, "auth.refresh_token_reuse_detected")


async def test_a_replacement_ended_by_signing_out_does_not_come_back(
    client: AsyncClient, make_user
):
    first = await _signed_in(client, make_user)
    second = _cookie(await _present(client, first))
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, second, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    assert (await _present(client, first)).status_code == 401


async def test_a_retired_token_of_a_deactivated_account_is_refused(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    await _present(client, first)
    async with db_session_factory() as db:
        await db.execute(update(User).values(is_active=False))
        await db.commit()

    assert (await _present(client, first)).status_code == 401


# ---- signing out ----


async def test_signing_out_ends_every_token_the_retry_left_live(
    client: AsyncClient, make_user, db_session_factory
):
    first = await _signed_in(client, make_user)
    await _present(client, first)
    latest = _cookie(await _present(client, first))  # two live tokens now
    async with db_session_factory() as db:
        live = (
            (await db.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))))
            .scalars()
            .all()
        )
    assert len(live) == 2

    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, latest, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    async with db_session_factory() as db:
        live = (
            (await db.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))))
            .scalars()
            .all()
        )
    assert live == []
