import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user
from src.api.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenPairResponse,
    UserRegisterRequest,
    UserResponse,
)
from src.core.db import get_db
from src.core.roles import Role
from src.core.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from src.models.refresh_token import RefreshToken
from src.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])

_invalid_credentials = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
)
_invalid_refresh_token = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A user with this email already exists")

    # Bootstrap: the very first user in an empty system becomes
    # SystemAdministrator so a fresh deployment is operable without manual DB
    # surgery. Every subsequent self-registration is deliberately capped at
    # the lowest-privilege role — the client cannot request a role, and
    # granting higher roles to other users is an admin action, not a
    # self-service one (there is no such endpoint yet in Phase 0).
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    role = Role.SYSTEM_ADMINISTRATOR if user_count == 0 else Role.READ_ONLY_AUDITOR

    user = User(email=payload.email, hashed_password=hash_password(payload.password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _issue_token_pair(
    db: AsyncSession, user: User, family_id: uuid.UUID | None = None
) -> TokenPairResponse:
    access = await create_access_token(db, user.id, role=user.role.value)
    refresh = await create_refresh_token(db, user.id, family_id=family_id)

    db.add(
        RefreshToken(
            id=refresh.jti,
            user_id=user.id,
            family_id=refresh.family_id,
            issued_at=datetime.now(UTC),
            expires_at=refresh.expires_at,
        )
    )
    await db.commit()

    return TokenPairResponse(access_token=access.token, refresh_token=refresh.token)


@router.post("/login", response_model=TokenPairResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenPairResponse:
    user = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.hashed_password):
        raise _invalid_credentials
    if not user.is_active:
        raise _invalid_credentials

    return await _issue_token_pair(db, user)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPairResponse:
    try:
        claims = await decode_token(db, payload.refresh_token, expected_type=TokenType.REFRESH)
    except InvalidTokenError as exc:
        raise _invalid_refresh_token from exc

    try:
        token_id = uuid.UUID(claims["jti"])
        family_id = uuid.UUID(claims["family_id"])
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise _invalid_refresh_token from exc

    stored = (
        await db.execute(select(RefreshToken).where(RefreshToken.id == token_id))
    ).scalar_one_or_none()
    if stored is None or stored.family_id != family_id or stored.user_id != user_id:
        raise _invalid_refresh_token

    now = datetime.now(UTC)

    if stored.revoked_at is not None:
        # This exact single-use token was already redeemed (or otherwise
        # revoked) once before — presenting it again means either the token
        # leaked or a client raced itself. Treat as compromise: revoke every
        # still-live token in the family so a stolen token can't keep
        # rotating alongside the legitimate client.
        await db.execute(
            RefreshToken.__table__.update()
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        raise _invalid_refresh_token

    if stored.expires_at < now:
        stored.revoked_at = now
        await db.commit()
        raise _invalid_refresh_token

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise _invalid_refresh_token

    stored.used_at = now
    stored.revoked_at = now
    await db.commit()

    return await _issue_token_pair(db, user, family_id=family_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    try:
        claims = await decode_token(db, payload.refresh_token, expected_type=TokenType.REFRESH)
        token_id = uuid.UUID(claims["jti"])
    except (InvalidTokenError, KeyError, ValueError):
        return None

    stored = (
        await db.execute(select(RefreshToken).where(RefreshToken.id == token_id))
    ).scalar_one_or_none()
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        await db.commit()

    return None


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
