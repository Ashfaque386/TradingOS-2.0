"""WebSocket authentication (Build Spec §18/§20's real-time UI wiring).

A browser's native `WebSocket` constructor cannot set an `Authorization`
header. Until Phase 8 the access token itself travelled in the URL
(`?token=`), where it is copied into proxy and server logs and browser
history. Now the page first buys a **one-time ticket** with its access token
(`POST /auth/ws-ticket`, an ordinary authenticated request) and opens the
socket with `?ticket=`: random, good for seconds, and spent by the first
connection that presents it (src.core.ws_ticket).

The handshake's `Origin` is checked too: a page on another site can open a
WebSocket to this server from the victim's browser, so an origin that is not
one of the console's (`CORS_ORIGINS`, or the host the request was addressed
to) is refused. A client that sends no Origin is not a browser page and has
no ambient credentials to abuse, so it is allowed to present a ticket.

This is a deliberate, narrow exception to `src.core.rbac`'s "every route goes
through `register_policy`/`require_role`" convention: that mechanism matches
on (HTTP method, route path) and WebSocket connections never go through
FastAPI's HTTP dependency-injection auth flow the same way, so each
WebSocket route authenticates explicitly via this helper instead, before it
accepts the connection (closing an unaccepted socket refuses the handshake).

Every channel here is read-only telemetry (activity feed, organization
events, the sign-off queue, ticks) -- there is no role-gated *write* path
over a WebSocket anywhere in this app, so a single "any authenticated
user" check is sufficient; a route needing finer-grained access would add
its own role check after calling this.
"""

from fastapi import WebSocket
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.web_session import origin_allowed
from src.core.ws_ticket import redeem_ws_ticket
from src.models.user import User

_POLICY_VIOLATION_CLOSE_CODE = 1008


async def authenticate_websocket(
    websocket: WebSocket, db: AsyncSession, redis: Redis
) -> User | None:
    """Validates the handshake's origin and its one-time `ticket` query param, and loads the user.
    Closes the socket and returns `None` on any failure -- the caller must check for `None` and
    return immediately without calling `accept()` (a closed socket cannot be re-accepted)."""
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="origin not allowed")
        return None

    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="missing ticket")
        return None

    user_id = await redeem_ws_ticket(redis, ticket)
    if user_id is None:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="invalid ticket")
        return None

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        await websocket.close(code=_POLICY_VIOLATION_CLOSE_CODE, reason="invalid user")
        return None

    return user
