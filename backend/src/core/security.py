import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any

import jwt
import structlog
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.core.config import get_settings
from src.models.jwt_signing_key import JwtSigningKey

settings = get_settings()
logger = structlog.get_logger(__name__)

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# JWTs are signed and verified with PyJWT. This module used to use python-jose,
# replaced in 2026-10 because of CVE-2026-85394 (an HS256 algorithm-confusion
# flaw with no fix version) and because python-jose dragged in `ecdsa`
# (CVE-2024-23342, also unfixable). The token format is the standard JWT/HS256
# one, so tokens issued by the old library keep verifying -- pinned by fixtures
# minted with python-jose in tests/test_security_jwt.py.


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class InvalidTokenError(Exception):
    """Raised when a JWT fails signature/expiry verification or has the wrong type."""


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(password, hashed_password)


# Argon2 is deliberately slow (tens of milliseconds of CPU). Called straight from an async route it
# holds the event loop for that long, so every other request, WebSocket and scheduled job waits
# behind each sign-in. The async versions run it on the thread pool instead; the sync ones stay for
# scripts.
async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


async def verify_password_async(password: str, hashed_password: str) -> bool:
    return await run_in_threadpool(verify_password, password, hashed_password)


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    return hash_password(secrets.token_urlsafe(24))


async def burn_password_check_time(password: str) -> None:
    """Spends the time a real password check takes, for a sign-in naming an email nobody has.

    Without it an unknown address is answered in microseconds and a known one in tens of
    milliseconds, which tells a caller which addresses have accounts without ever guessing a
    password. The hash is a throwaway made with the same parameters as a real one; the result is
    discarded."""
    await verify_password_async(password, _decoy_hash())


# Phase 21 (docs/phase20-old-vs-new-comparison.md item 11): the JWT
# signing key used to be read once from `settings.jwt_secret_key` at
# import time -- correct for a static env var, but that makes a "rotate"
# endpoint pointless, since nothing in this process would ever re-read it
# without a restart (the same "resolves once at process startup" gotcha
# already documented for `build_tick_source()` in Phase 8). The real key
# now lives in the `jwt_signing_keys` table and is cached in-process here,
# with `invalidate_signing_key_cache()` the one place that clears it --
# called by `POST /system/jwt-signing-key/rotate` right after writing a
# new row, so a rotation takes effect on this process's very next request,
# no restart required.
_cached_key: str | None = None

# An HMAC signing key shorter than this is guessable by brute force, and PyJWT
# warns about it. The one value this repository ships as a default
# (`.env.example`, docker-compose.yml, `Settings.jwt_secret_key`) is public.
MIN_SIGNING_KEY_BYTES = 32
PLACEHOLDER_SIGNING_KEY = "change-me-in-production"


def signing_key_weakness(key: str) -> str | None:
    """Why a signing key must not be trusted, or None if it is fine."""
    if key == PLACEHOLDER_SIGNING_KEY:
        return "it is the public placeholder this repository ships as its default"
    if len(key.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
        return f"it is shorter than {MIN_SIGNING_KEY_BYTES} bytes"
    return None


def invalidate_signing_key_cache() -> None:
    global _cached_key
    _cached_key = None


async def get_current_signing_key(db: AsyncSession) -> str:
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    row = (
        await db.execute(select(JwtSigningKey).order_by(JwtSigningKey.created_at.desc()).limit(1))
    ).scalar_one_or_none()

    if row is None:
        # First-ever call in this deployment's lifetime: seed from the
        # pre-Phase-21 env var so every token/session issued before this
        # phase's migration keeps validating across the upgrade -- unless that
        # value is the public placeholder (or too short to be a key), which
        # would make every token forgeable by anyone who read the repository.
        # `.env.example` and docker-compose.yml both ship the placeholder, so
        # a stack started with defaults used to be seeded with it, and the
        # database row then wins over `.env` forever. Generate a real key.
        env_key = settings.jwt_secret_key
        weakness = signing_key_weakness(env_key)
        if weakness is None:
            seed_key, origin = env_key, "env-seed"
        else:
            seed_key, origin = secrets.token_urlsafe(48), "generated-at-first-start"
            logger.warning("security.jwt_env_key_not_used", reason=weakness)
        row = JwtSigningKey(key=seed_key, created_at=datetime.now(UTC), created_by=origin)
        db.add(row)
        await db.commit()

    _cached_key = row.key
    return _cached_key


@dataclass(frozen=True, slots=True)
class SigningKeyStatus:
    """What an operator may know about the signing key -- never its value."""

    created_at: datetime
    created_by: str
    weakness: str | None


async def get_signing_key_status(db: AsyncSession) -> SigningKeyStatus:
    await get_current_signing_key(db)  # makes sure a row exists
    row = (
        await db.execute(select(JwtSigningKey).order_by(JwtSigningKey.created_at.desc()).limit(1))
    ).scalar_one()
    return SigningKeyStatus(
        created_at=row.created_at,
        created_by=row.created_by,
        weakness=signing_key_weakness(row.key),
    )


async def warn_if_signing_key_weak(db: AsyncSession) -> None:
    """Startup check. A weak key already stored in the database cannot be
    swapped silently -- replacing it signs everyone out -- so say so, loudly,
    and where the fix is."""
    status = await get_signing_key_status(db)
    if status.weakness is not None:
        logger.warning(
            "security.jwt_signing_key_weak",
            reason=status.weakness,
            created_by=status.created_by,
            fix="Settings > Security > Rotate, or POST /api/v1/system/jwt-signing-key/rotate",
        )


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken(IssuedToken):
    family_id: uuid.UUID


async def create_access_token(db: AsyncSession, user_id: uuid.UUID, role: str) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    jti = uuid.uuid4()
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": TokenType.ACCESS.value,
        "iat": now,
        "exp": expires_at,
        "jti": str(jti),
    }
    key = await get_current_signing_key(db)
    token = jwt.encode(payload, key, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


async def create_refresh_token(
    db: AsyncSession, user_id: uuid.UUID, family_id: uuid.UUID | None = None
) -> IssuedRefreshToken:
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    jti = uuid.uuid4()
    resolved_family_id = family_id or uuid.uuid4()
    payload = {
        "sub": str(user_id),
        "type": TokenType.REFRESH.value,
        "iat": now,
        "exp": expires_at,
        "jti": str(jti),
        "family_id": str(resolved_family_id),
    }
    key = await get_current_signing_key(db)
    token = jwt.encode(payload, key, algorithm=settings.jwt_algorithm)
    return IssuedRefreshToken(
        token=token, jti=jti, expires_at=expires_at, family_id=resolved_family_id
    )


async def decode_token(db: AsyncSession, token: str, expected_type: TokenType) -> dict[str, Any]:
    """Validates against the current signing key only -- a hard cutover,
    on purpose: rotation is a "this may be compromised, end every session
    now" action (see the rotate endpoint's own docstring), not something
    that should leave a grace window where a token signed with the old key
    still works."""
    key = await get_current_signing_key(db)
    try:
        # `algorithms` is a single pinned value, never derived from the token's
        # own header -- the alg=none and HS384/HS512-with-the-same-secret tokens
        # are rejected by this (tests/test_security_jwt.py).
        payload = jwt.decode(token, key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != expected_type.value:
        raise InvalidTokenError(f"expected token type {expected_type.value!r}")

    return payload
