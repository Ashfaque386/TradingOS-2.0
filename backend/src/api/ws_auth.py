"""WebSocket authentication (Build Spec §18/§20's real-time UI wiring).

A browser's native `WebSocket` constructor cannot set an `Authorization`
header, so the JWT access token travels as a `?token=` query parameter
instead -- the standard, well-established pattern for browser WebSocket
auth. This is a deliberate, narrow exception to `src.core.rbac`'s "every
route goes through `register_policy`/`require_role`" convention: that
mechanism matches on (HTTP method, route path) and WebSocket connections
never go through FastAPI's HTTP dependency-injection auth flow the same
way, so each WebSocket route authenticates explicitly via this helper
instead, right after `accept()`.

Every channel here is read-only telemetry (activity feed, organization
events, the sign-off queue, ticks) -- there is no role-gated *write* path
over a WebSocket anywhere in this app, so a single "any authenticated
user" check is sufficient; a route needing finer-grained access would add
its own role check after calling this.
"""

import uuid

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import InvalidTokenError, TokenType, decode_token
from src.models.user import User

_POLICY_VIOLATION_CLOSE_CODE = 1008


async def authenticate_websocket(websocket: WebSocket, db: AsyncSession) -> User | None:
    """Validates the `token` query param and loads the user. Closes the
    socket and returns `None` on any failure -- the caller must check for
    `None` and return immediately without ever calling `accept()` again
    (a closed socket cannot be re-accepted)."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="missing token")
        return None

    try:
        payload = decode_token(token, expected_type=TokenType.ACCESS)
    except InvalidTokenError:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="invalid token")
        return None

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="invalid token")
        return None

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="invalid user")
        return None

    return user
