"""A deployment must not sign sessions with a key anyone can read.

`.env.example` and docker-compose.yml both ship `change-me-in-production`, and
the first request in a deployment's life used to seed the `jwt_signing_keys`
row from it -- after which the database row wins over `.env` for good, so
editing `.env` later changed nothing and every token was forgeable by anyone who
had read the repository. These tests pin the three things that now stand in the
way: bootstrap never seeds a weak key, an already-stored weak key is reported
(never revealed) to an administrator, and startup says so in the log.
"""

import datetime as dt
import secrets
from typing import Any

import pytest
from sqlalchemy import select

from src.core import security
from src.core.roles import Role
from src.core.security import (
    MIN_SIGNING_KEY_BYTES,
    PLACEHOLDER_SIGNING_KEY,
    get_current_signing_key,
    invalidate_signing_key_cache,
    signing_key_weakness,
    warn_if_signing_key_weak,
)
from src.models.jwt_signing_key import JwtSigningKey


async def _login(client, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _store_key(db_session_factory, key: str) -> None:
    async with db_session_factory() as db:
        db.add(JwtSigningKey(key=key, created_at=dt.datetime.now(dt.UTC), created_by="test"))
        await db.commit()
    invalidate_signing_key_cache()


class _RecordingLogger:
    """Keeps warnings apart from everything else: a security finding logged at
    `info` would scroll past unseen."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []  # warnings
        self.quieter: list[str] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.quieter.append(event)


@pytest.mark.parametrize(
    ("key", "weak"),
    [
        (PLACEHOLDER_SIGNING_KEY, True),
        ("short", True),
        ("x" * (MIN_SIGNING_KEY_BYTES - 1), True),
        ("x" * MIN_SIGNING_KEY_BYTES, False),
        (secrets.token_urlsafe(48), False),
    ],
)
def test_what_counts_as_a_weak_signing_key(key: str, weak: bool):
    assert (signing_key_weakness(key) is not None) is weak


async def test_bootstrap_never_seeds_the_public_placeholder(db_session_factory, monkeypatch):
    monkeypatch.setattr(security.settings, "jwt_secret_key", PLACEHOLDER_SIGNING_KEY)
    invalidate_signing_key_cache()

    async with db_session_factory() as db:
        key = await get_current_signing_key(db)

    assert key != PLACEHOLDER_SIGNING_KEY and signing_key_weakness(key) is None
    async with db_session_factory() as db:
        row = (await db.execute(select(JwtSigningKey))).scalar_one()
    assert row.key == key and row.created_by == "generated-at-first-start"


async def test_bootstrap_keeps_a_strong_key_from_the_environment(db_session_factory, monkeypatch):
    """Sessions issued before the signing key moved into the database keep
    validating across that upgrade -- when the environment's key was a real one."""
    strong = "k" * 40
    monkeypatch.setattr(security.settings, "jwt_secret_key", strong)
    invalidate_signing_key_cache()

    async with db_session_factory() as db:
        key = await get_current_signing_key(db)

    assert key == strong


async def test_status_flags_a_weak_stored_key_and_never_returns_it(
    client, make_user, db_session_factory
):
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    await _store_key(db_session_factory, PLACEHOLDER_SIGNING_KEY)
    headers = await _login(client, "admin@example.com")

    response = await client.get("/api/v1/system/jwt-signing-key", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"created_at", "created_by", "weak", "weakness"}
    assert body["weak"] is True and "placeholder" in body["weakness"]
    assert body["created_by"] == "test"
    assert PLACEHOLDER_SIGNING_KEY not in response.text


async def test_rotating_makes_the_status_healthy(client, make_user, db_session_factory):
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    await _store_key(db_session_factory, PLACEHOLDER_SIGNING_KEY)
    headers = await _login(client, "admin@example.com")
    assert (await client.get("/api/v1/system/jwt-signing-key", headers=headers)).json()["weak"]

    rotated = await client.post("/api/v1/system/jwt-signing-key/rotate", headers=headers)
    assert rotated.status_code == 200
    headers = await _login(client, "admin@example.com")  # the old session is gone by design

    body = (await client.get("/api/v1/system/jwt-signing-key", headers=headers)).json()
    assert body["weak"] is False and body["weakness"] is None
    assert body["created_by"] == "admin@example.com"


async def test_status_is_for_administrators_only(client, make_user):
    await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    headers = await _login(client, "pm@example.com")

    assert (await client.get("/api/v1/system/jwt-signing-key", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/system/jwt-signing-key")).status_code == 401


async def test_startup_warns_about_a_weak_stored_key_and_is_quiet_otherwise(
    db_session_factory, monkeypatch
):
    log = _RecordingLogger()
    monkeypatch.setattr(security, "logger", log)

    await _store_key(db_session_factory, PLACEHOLDER_SIGNING_KEY)
    async with db_session_factory() as db:
        await warn_if_signing_key_weak(db)
    ((event, fields),) = log.events
    assert event == "security.jwt_signing_key_weak" and "placeholder" in fields["reason"]
    assert "rotate" in fields["fix"].lower()
    assert PLACEHOLDER_SIGNING_KEY not in repr(fields)

    log.events.clear()
    await _store_key(db_session_factory, secrets.token_urlsafe(48))
    async with db_session_factory() as db:
        await warn_if_signing_key_weak(db)
    assert log.events == []
