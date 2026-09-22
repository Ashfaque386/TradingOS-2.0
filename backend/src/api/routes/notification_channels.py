"""Notification channel config API (Build Spec §18, §20): write-only for
every secret field, same posture as src.api.routes.broker_credentials --
there is no `GET` that returns a `bot_token`/`webhook_url`/
`webhook_secret_token`/`public_key`/`signing_secret`, only whether a
channel is configured plus its non-secret allowlist/alert-level config.

detect-chat-id and test are new (Settings redesign): neither existed
before -- src.notifications.senders only ever sent through a persisted
NotificationChannelConfig via the alert-dispatch path
(src.notifications.dispatch.notify), with no "try this before you save
it" capability and no way to discover a Telegram chat_id at all. Both
reuse the real senders/HTTP calls a saved config would use, not a
simulated success.
"""

from datetime import UTC, datetime

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    DetectTelegramChatIdRequest,
    DetectTelegramChatIdResponse,
    NotificationChannelStatusResponse,
    TestNotificationChannelRequest,
    TestNotificationChannelResponse,
    WriteNotificationChannelRequest,
)
from src.audit.service import write_audit_entry
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.notifications.channel_store import (
    NotificationChannelConfig,
    NotificationChannelStore,
    NotificationChannelStoreError,
    get_notification_channel_store,
)
from src.notifications.senders import send_to_channel
from src.notifications.types import NotificationChannel

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/notification-channels", tags=["notification-channels"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/notification-channels", roles=list(Role))
register_policy("POST", "/api/v1/notification-channels/{channel}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/notification-channels/{channel}", roles=_WRITE_ROLES)
register_policy("POST", "/api/v1/notification-channels/{channel}/test", roles=_WRITE_ROLES)
register_policy("POST", "/api/v1/notification-channels/telegram/detect-chat-id", roles=_WRITE_ROLES)


def get_channel_store() -> NotificationChannelStore:
    try:
        return get_notification_channel_store()
    except NotificationChannelStoreError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def get_telegram_http_client():
    """A FastAPI dependency, not a bare `httpx.AsyncClient(...)` call, so
    tests can override it with a client bound to httpx.MockTransport --
    same "real code, injected transport in tests" posture as
    src.brokers.zerodha/upstox and src.api.routes.broker_oauth."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


def _require_known_channel(channel: str) -> NotificationChannel:
    try:
        return NotificationChannel(channel)
    except ValueError as exc:
        known = ", ".join(c.value for c in NotificationChannel)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown channel: {channel!r} (known: {known})"
        ) from exc


@router.get("")
async def list_notification_channel_status_endpoint(
    _current_user: User = Depends(require_role),
    store: NotificationChannelStore = Depends(get_channel_store),
) -> list[NotificationChannelStatusResponse]:
    result = []
    for channel in NotificationChannel:
        config = store.get_config(channel)
        result.append(
            NotificationChannelStatusResponse(
                channel=channel.value,
                configured=config is not None,
                enabled=config.enabled if config is not None else False,
                allowed_sender_ids=config.allowed_sender_ids if config is not None else [],
                alert_levels=config.alert_levels if config is not None else [],
            )
        )
    return result


@router.post("/{channel}", status_code=status.HTTP_204_NO_CONTENT)
async def write_notification_channel_endpoint(
    channel: str,
    body: WriteNotificationChannelRequest,
    current_user: User = Depends(require_role),
    store: NotificationChannelStore = Depends(get_channel_store),
    db: AsyncSession = Depends(get_db),
) -> None:
    parsed_channel = _require_known_channel(channel)
    store.set_config(
        parsed_channel,
        NotificationChannelConfig(
            enabled=body.enabled,
            bot_token=body.bot_token,
            chat_id=body.chat_id,
            webhook_url=body.webhook_url,
            webhook_secret_token=body.webhook_secret_token,
            public_key=body.public_key,
            signing_secret=body.signing_secret,
            allowed_sender_ids=body.allowed_sender_ids,
            alert_levels=body.alert_levels,
        ),
    )
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="notification_channels.updated",
        entity_type="notification_channel",
        entity_id=channel,
        details={"enabled": body.enabled, "alert_levels": body.alert_levels},
    )
    await db.commit()
    logger.info("notification_channels.updated", channel=channel, updated_by=str(current_user.id))


@router.delete("/{channel}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_channel_endpoint(
    channel: str,
    current_user: User = Depends(require_role),
    store: NotificationChannelStore = Depends(get_channel_store),
    db: AsyncSession = Depends(get_db),
) -> None:
    parsed_channel = _require_known_channel(channel)
    store.delete_config(parsed_channel)
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="notification_channels.deleted",
        entity_type="notification_channel",
        entity_id=channel,
    )
    await db.commit()
    logger.info("notification_channels.deleted", channel=channel, deleted_by=str(current_user.id))


@router.post("/{channel}/test")
async def test_notification_channel_endpoint(
    channel: str,
    body: TestNotificationChannelRequest,
    current_user: User = Depends(require_role),
    store: NotificationChannelStore = Depends(get_channel_store),
    db: AsyncSession = Depends(get_db),
) -> TestNotificationChannelResponse:
    parsed_channel = _require_known_channel(channel)
    saved = store.get_config(parsed_channel)
    config = NotificationChannelConfig(
        enabled=True,
        bot_token=body.bot_token or (saved.bot_token if saved else None),
        chat_id=body.chat_id or (saved.chat_id if saved else None),
        webhook_url=body.webhook_url or (saved.webhook_url if saved else None),
    )
    result = await send_to_channel(
        parsed_channel,
        config,
        "TradingOS test message -- if you can see this, this channel is wired up correctly.",
    )
    tested_at = datetime.now(UTC).isoformat()
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="notification_channels.tested",
        entity_type="notification_channel",
        entity_id=channel,
        details={"ok": result.ok, "status_code": result.status_code},
    )
    await db.commit()
    logger.info(
        "notification_channels.tested",
        channel=channel,
        tested_by=str(current_user.id),
        ok=result.ok,
    )
    return TestNotificationChannelResponse(
        channel=channel,
        ok=result.ok,
        status_code=result.status_code,
        error=result.error,
        tested_at=tested_at,
    )


@router.post("/telegram/detect-chat-id")
async def detect_telegram_chat_id_endpoint(
    body: DetectTelegramChatIdRequest,
    _current_user: User = Depends(require_role),
    client: httpx.AsyncClient = Depends(get_telegram_http_client),
) -> DetectTelegramChatIdResponse:
    """Calls Telegram's real getUpdates API and returns the most recent
    chat that messaged the bot -- the operator's own guided step is
    "message your bot once, then click Detect", the standard way to learn
    a chat_id without any inbound webhook already being configured."""
    try:
        resp = await client.get(
            f"https://api.telegram.org/bot{body.bot_token}/getUpdates",
            params={"limit": 5, "offset": -5},
        )
    except httpx.HTTPError as exc:
        return DetectTelegramChatIdResponse(ok=False, detail=f"could not reach Telegram: {exc}")

    if resp.status_code != 200:
        return DetectTelegramChatIdResponse(
            ok=False, detail=f"Telegram returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    data = resp.json()
    if not data.get("ok"):
        return DetectTelegramChatIdResponse(
            ok=False, detail=data.get("description", "Telegram reported an error")
        )

    updates = data.get("result", [])
    for update in reversed(updates):
        chat = (
            (update.get("message") or {}).get("chat")
            or (update.get("channel_post") or {}).get("chat")
            or (update.get("my_chat_member") or {}).get("chat")
        )
        if chat and "id" in chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name")
            return DetectTelegramChatIdResponse(
                ok=True,
                chat_id=str(chat["id"]),
                chat_label=label,
                detail="found a recent chat",
            )

    return DetectTelegramChatIdResponse(
        ok=False,
        detail=(
            "no messages found -- send your bot a message (or add it to a "
            "group/channel), then try again"
        ),
    )
