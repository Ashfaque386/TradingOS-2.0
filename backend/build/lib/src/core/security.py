import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.models.jwt_signing_key import JwtSigningKey

settings = get_settings()

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class InvalidTokenError(Exception):
    """Raised when a JWT fails signature/expiry verification or has the wrong type."""


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(password, hashed_password)


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
        # phase's migration keeps validating across the upgrade.
        row = JwtSigningKey(
            key=settings.jwt_secret_key, created_at=datetime.now(UTC), created_by="env-seed"
        )
        db.add(row)
        await db.commit()

    _cached_key = row.key
    return _cached_key


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


async def decode_token(db: AsyncSession, token: str, expected_type: TokenType) -> dict:
    """Validates against the current signing key only -- a hard cutover,
    on purpose: rotation is a "this may be compromised, end every session
    now" action (see the rotate endpoint's own docstring), not something
    that should leave a grace window where a token signed with the old key
    still works."""
    key = await get_current_signing_key(db)
    try:
        payload = jwt.decode(token, key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != expected_type.value:
        raise InvalidTokenError(f"expected token type {expected_type.value!r}")

    return payload
