"""Inbound channel webhooks (Build Spec §18): one route per channel, each
enforcing, in this fixed order, before anything is routed anywhere:

1. **Signature verification** (src.notifications.verification) -- proves
   this request genuinely came from Telegram/Discord/Slack.
2. **Replay guard** (src.notifications.replay_guard) -- proves this exact
   message hasn't already been processed (a signed request can still be a
   replay of an old, once-valid one).
3. **Rate limit** (src.notifications.replay_guard) -- caps how many
   requests per channel per window get to step 4 at all.
4. **Routing** (src.notifications.inbound_router) -- verified-sender
   check, classification, and either a spawned organization run or a
   direct CEO Agent reply, sent back out through the same channel.

Deliberately **not** RBAC-gated (no `Depends(require_role)`/
`register_policy` call) -- these are external services calling in with no
JWT to present; the signature check above is this route's actual auth
mechanism, the same way it would be for any real webhook receiver.

Every route reads the **raw request body** before any JSON parsing --
signature verification is defined over the exact bytes the sender signed,
which `request.json()` would already have discarded.

**Discord/Slack setup handshakes**: Discord sends a `PING` (`type: 1`)
interaction the first time an Interactions Endpoint URL is registered and
expects `{"type": 1}` back, signed-and-verified like any other request;
Slack sends a `url_verification` event with a `challenge` string during
Events API setup and expects that exact string echoed back. Both are
handled here as real, necessary parts of onboarding each channel, not
special-cased away.
"""

import json

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.db import get_session_factory
from src.core.redis_client import get_redis
from src.notifications.channel_store import (
    NotificationChannelStore,
    NotificationChannelStoreError,
    get_notification_channel_store,
)
from src.notifications.inbound_router import route_inbound_message
from src.notifications.replay_guard import check_and_record_replay, check_rate_limit
from src.notifications.types import NotificationChannel
from src.notifications.verification import (
    verify_discord_signature,
    verify_slack_signature,
    verify_telegram_secret,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def get_channel_store_or_none() -> NotificationChannelStore | None:
    try:
        return get_notification_channel_store()
    except NotificationChannelStoreError:
        return None


def _reject(reason: str, channel: str) -> Response:
    logger.warning("webhooks.rejected", channel=channel, reason=reason)
    return Response(status_code=status.HTTP_401_UNAUTHORIZED, content=reason)


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    store: NotificationChannelStore | None = Depends(get_channel_store_or_none),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    body = await request.body()
    if store is None:
        return _reject("no notification channels configured", "telegram")
    config = store.get_config(NotificationChannel.TELEGRAM)
    if config is None or not config.enabled:
        return _reject("telegram channel not configured", "telegram")

    if not verify_telegram_secret(config, x_telegram_bot_api_secret_token):
        return _reject("invalid secret token", "telegram")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="invalid JSON")

    update_id = str(payload.get("update_id", ""))
    message = payload.get("message") or {}
    sender_id = str((message.get("from") or {}).get("id", ""))
    text = message.get("text", "")

    if not update_id or not sender_id or not text:
        return Response(status_code=status.HTTP_200_OK, content="ignored: not a text message")

    if not await check_and_record_replay(redis, channel="telegram", message_id=update_id):
        return _reject("replayed update_id", "telegram")
    if not await check_rate_limit(redis, channel="telegram", sender_id=sender_id):
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    result = await route_inbound_message(
        session_factory,
        redis,
        store,
        channel=NotificationChannel.TELEGRAM,
        sender_id=sender_id,
        text=text,
    )
    return Response(
        status_code=status.HTTP_200_OK if result.accepted else status.HTTP_403_FORBIDDEN,
        content=json.dumps({"accepted": result.accepted, "reason": result.reason}),
        media_type="application/json",
    )


@router.post("/discord")
async def discord_webhook(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    store: NotificationChannelStore | None = Depends(get_channel_store_or_none),
    x_signature_ed25519: str | None = Header(default=None),
    x_signature_timestamp: str | None = Header(default=None),
) -> Response:
    body = await request.body()
    if store is None:
        return _reject("no notification channels configured", "discord")
    config = store.get_config(NotificationChannel.DISCORD)
    if config is None or not config.enabled:
        return _reject("discord channel not configured", "discord")

    if not verify_discord_signature(
        config,
        signature_header=x_signature_ed25519,
        timestamp_header=x_signature_timestamp,
        body=body,
    ):
        return _reject("invalid Ed25519 signature", "discord")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="invalid JSON")

    # Discord's interactions handshake: type 1 = PING, must be echoed back
    # once the signature above has already verified this really came from
    # Discord -- not a separate, unauthenticated fast path.
    if payload.get("type") == 1:
        return Response(
            status_code=status.HTTP_200_OK,
            content=json.dumps({"type": 1}),
            media_type="application/json",
        )

    interaction_id = str(payload.get("id", ""))
    sender_id = str(((payload.get("member") or {}).get("user") or {}).get("id", ""))
    data = payload.get("data") or {}
    options = data.get("options") or []
    text = " ".join(str(opt.get("value", "")) for opt in options) or data.get("name", "")

    if not interaction_id or not sender_id or not text:
        return Response(status_code=status.HTTP_200_OK, content="ignored: no actionable text")

    if not await check_and_record_replay(redis, channel="discord", message_id=interaction_id):
        return _reject("replayed interaction id", "discord")
    if not await check_rate_limit(redis, channel="discord", sender_id=sender_id):
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    result = await route_inbound_message(
        session_factory,
        redis,
        store,
        channel=NotificationChannel.DISCORD,
        sender_id=sender_id,
        text=text,
    )
    return Response(
        status_code=status.HTTP_200_OK if result.accepted else status.HTTP_403_FORBIDDEN,
        content=json.dumps({"accepted": result.accepted, "reason": result.reason}),
        media_type="application/json",
    )


@router.post("/slack")
async def slack_webhook(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    store: NotificationChannelStore | None = Depends(get_channel_store_or_none),
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
) -> Response:
    body = await request.body()
    if store is None:
        return _reject("no notification channels configured", "slack")
    config = store.get_config(NotificationChannel.SLACK)
    if config is None or not config.enabled:
        return _reject("slack channel not configured", "slack")

    if not verify_slack_signature(
        config,
        signature_header=x_slack_signature,
        timestamp_header=x_slack_request_timestamp,
        body=body,
    ):
        return _reject("invalid HMAC signature", "slack")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="invalid JSON")

    # Slack's Events API setup handshake: echo the challenge back verbatim,
    # again only after the signature above has already verified the caller.
    if payload.get("type") == "url_verification":
        return Response(content=payload.get("challenge", ""), media_type="text/plain")

    event = payload.get("event") or {}
    sender_id = str(event.get("user", ""))
    text = event.get("text", "")
    message_ts = str(event.get("ts", ""))

    # Slack echoes a bot's own outbound messages back through this same
    # events pipe when the app is a channel member -- bot_id is present
    # only on those, never on a genuine human message.
    if not message_ts or not sender_id or not text or event.get("bot_id"):
        return Response(status_code=status.HTTP_200_OK, content="ignored: not a human text message")

    if not await check_and_record_replay(redis, channel="slack", message_id=message_ts):
        return _reject("replayed message ts", "slack")
    if not await check_rate_limit(redis, channel="slack", sender_id=sender_id):
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    result = await route_inbound_message(
        session_factory,
        redis,
        store,
        channel=NotificationChannel.SLACK,
        sender_id=sender_id,
        text=text,
    )
    return Response(
        status_code=status.HTTP_200_OK if result.accepted else status.HTTP_403_FORBIDDEN,
        content=json.dumps({"accepted": result.accepted, "reason": result.reason}),
        media_type="application/json",
    )
