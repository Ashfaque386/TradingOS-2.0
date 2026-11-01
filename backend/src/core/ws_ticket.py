"""One-time tickets for opening a WebSocket.

A browser cannot put an `Authorization` header on a WebSocket handshake, so until Phase 8 the access
token itself was sent in the URL (`?token=`), where it ends up in proxy and server logs, in browser
history and in a copied link. Now the page buys a ticket with its access token over an ordinary
authenticated request (`POST /auth/ws-ticket`) and puts only the ticket in the URL. A ticket

- is random and carries nothing (a key to a Redis entry, stored hashed, so a copy of Redis does not
  hold usable tickets);
- lives `WS_TICKET_TTL_SECONDS` (30 by default), long enough to open the socket and no longer;
- works once: redeeming it deletes it in the same step, so a logged URL is already spent.
"""

import hashlib
import secrets
import uuid
from typing import cast

from redis.asyncio import Redis

_KEY_PREFIX = "auth:ws-ticket:"


def _key(ticket: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(ticket.encode("utf-8")).hexdigest()


async def issue_ws_ticket(redis: Redis, user_id: uuid.UUID, ttl_seconds: int) -> str:
    ticket = secrets.token_urlsafe(32)
    await redis.set(_key(ticket), str(user_id), ex=ttl_seconds)
    return ticket


async def redeem_ws_ticket(redis: Redis, ticket: str) -> uuid.UUID | None:
    """The user the ticket was issued to, or None if it is unknown, expired or already used."""
    raw = cast("bytes | str | None", await redis.getdel(_key(ticket)))
    if raw is None:
        return None
    try:
        return uuid.UUID(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except ValueError:
        return None
