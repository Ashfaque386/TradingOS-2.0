"""Notification channel config API (Build Spec §18, §20): write-only for
every secret field, same posture as src.api.routes.broker_credentials --
there is no `GET` that returns a `bot_token`/`webhook_url`/
`webhook_secret_token`/`public_key`/`signing_secret`, only whether a
channel is configured plus its non-secret allowlist/alert-level config.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from src.api.schemas import NotificationChannelStatusResponse, WriteNotificationChannelRequest
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.notifications.channel_store import (
    NotificationChannelConfig,
    NotificationChannelStore,
    NotificationChannelStoreError,
    get_notification_channel_store,
)
from src.notifications.types import NotificationChannel

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/notification-channels", tags=["notification-channels"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/notification-channels", roles=list(Role))
register_policy("POST", "/api/v1/notification-channels/{channel}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/notification-channels/{channel}", roles=_WRITE_ROLES)


def get_channel_store() -> NotificationChannelStore:
    try:
        return get_notification_channel_store()
    except NotificationChannelStoreError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


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
    logger.info("notification_channels.updated", channel=channel, updated_by=str(current_user.id))


@router.delete("/{channel}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_channel_endpoint(
    channel: str,
    current_user: User = Depends(require_role),
    store: NotificationChannelStore = Depends(get_channel_store),
) -> None:
    parsed_channel = _require_known_channel(channel)
    store.delete_config(parsed_channel)
    logger.info("notification_channels.deleted", channel=channel, deleted_by=str(current_user.id))
