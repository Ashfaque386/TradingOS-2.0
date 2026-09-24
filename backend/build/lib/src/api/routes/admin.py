"""Admin user management (Phase 21,
docs/phase20-old-vs-new-comparison.md item 10): the only way to create a
user at any role other than the self-registration bootstrap-admin-or-
ReadOnlyAuditor rule (`POST /auth/register`, see its own docstring), or to
change an existing user's role, deactivate/reactivate an account, or force
a compromised account's sessions to end. Everything here is
SystemAdministrator-only and, being a mutation, picked up automatically by
`AuditLoggingMiddleware` -- no route below writes its own audit entry on
top of that generic one, matching the rest of this codebase's posture
that the blanket middleware is enough unless a route has business-specific
detail worth adding (none of these do).

**Guard against total self-lockout**: deactivating a user, or changing
their role away from SystemAdministrator, is refused with a 400 if doing
so would leave zero active SystemAdministrator accounts -- not just a
self-service check (an admin acting on a *different* admin account could
just as easily lock the whole system out).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    AdminChangeRoleRequest,
    AdminCreateUserRequest,
    AdminRevokeSessionsResponse,
    UserResponse,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.core.security import hash_password
from src.models.refresh_token import RefreshToken
from src.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN_ONLY = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/admin/ping", roles=_ADMIN_ONLY)
register_policy("GET", "/api/v1/admin/users", roles=_ADMIN_ONLY)
register_policy("POST", "/api/v1/admin/users", roles=_ADMIN_ONLY)
register_policy("PATCH", "/api/v1/admin/users/{user_id}/role", roles=_ADMIN_ONLY)
register_policy("POST", "/api/v1/admin/users/{user_id}/deactivate", roles=_ADMIN_ONLY)
register_policy("POST", "/api/v1/admin/users/{user_id}/reactivate", roles=_ADMIN_ONLY)
register_policy("POST", "/api/v1/admin/users/{user_id}/revoke-sessions", roles=_ADMIN_ONLY)


@router.get("/ping")
async def ping(current_user: User = Depends(require_role)) -> dict:
    return {"status": "ok", "role": current_user.role.value}


async def _get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


async def _active_admin_count(db: AsyncSession, *, excluding: uuid.UUID | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.role == Role.SYSTEM_ADMINISTRATOR, User.is_active.is_(True))
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return (await db.execute(stmt)).scalar_one()


def _would_remove_last_admin(user: User) -> bool:
    return user.role == Role.SYSTEM_ADMINISTRATOR and user.is_active


@router.get("/users")
async def list_users_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[UserResponse]:
    rows = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [UserResponse.model_validate(row) for row in rows]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(
    body: AdminCreateUserRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> UserResponse:
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A user with this email already exists")

    user = User(email=body.email, hashed_password=hash_password(body.password), role=body.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.patch("/users/{user_id}/role")
async def change_user_role_endpoint(
    user_id: uuid.UUID,
    body: AdminChangeRoleRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> UserResponse:
    user = await _get_user_or_404(db, user_id)

    if _would_remove_last_admin(user) and body.role != Role.SYSTEM_ADMINISTRATOR:
        if await _active_admin_count(db, excluding=user.id) == 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Refusing to change this user's role: they are the last active "
                "SystemAdministrator. Promote another user first.",
            )

    user.role = body.role
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/deactivate")
async def deactivate_user_endpoint(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> UserResponse:
    user = await _get_user_or_404(db, user_id)

    if _would_remove_last_admin(user) and await _active_admin_count(db, excluding=user.id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Refusing to deactivate this user: they are the last active "
            "SystemAdministrator. Promote another user first.",
        )

    user.is_active = False
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/reactivate")
async def reactivate_user_endpoint(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> UserResponse:
    user = await _get_user_or_404(db, user_id)
    user.is_active = True
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions_endpoint(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> AdminRevokeSessionsResponse:
    """Marks every one of this user's not-already-revoked refresh tokens
    revoked, same effect a real security incident response needs: their
    current access token still works until it expires (at most
    `settings.access_token_expire_minutes`), but no refresh token can mint
    a new one afterward -- combined with `deactivate` above (which also
    blocks login and every RBAC-gated route via `get_current_user`'s own
    `is_active` check) for a full, immediate lockout."""
    await _get_user_or_404(db, user_id)

    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    await db.commit()
    return AdminRevokeSessionsResponse(user_id=user_id, revoked_count=result.rowcount)
