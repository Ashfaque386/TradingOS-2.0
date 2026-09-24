"""notification-send (Build Spec §16 starter skill set, wired to real
channels in Phase 12 per Build Spec §18). The only async entry in
src.agents.tools.registry.SKILLS -- see that module's SkillFn type for
why the registry can execute both sync and async skills.

An agent-invoked ad hoc send (params: {"channel": "telegram"|"discord"|
"slack", "message": str}), distinct from src.notifications.dispatch.notify
-- that function fans a *system* alert out to every channel subscribed to
one of the four fixed alert levels; this skill sends one message to one
specific channel an agent names, using whatever config that channel
already has stored (src.notifications.channel_store).
"""

from src.notifications.channel_store import (
    NotificationChannelStoreError,
    get_notification_channel_store,
)
from src.notifications.senders import send_to_channel
from src.notifications.types import NotificationChannel


async def notification_send(params: dict) -> dict:
    channel_name = params.get("channel")
    message = params.get("message", "")

    try:
        channel = NotificationChannel(channel_name)
    except ValueError:
        return {
            "sent": False,
            "channel": channel_name,
            "error": f"unknown channel: {channel_name!r}",
        }

    try:
        store = get_notification_channel_store()
    except NotificationChannelStoreError as exc:
        return {"sent": False, "channel": channel_name, "error": str(exc)}

    config = store.get_config(channel)
    if config is None or not config.enabled:
        return {"sent": False, "channel": channel_name, "error": "channel not configured"}

    result = await send_to_channel(channel, config, message)
    return {"sent": result.ok, "channel": channel_name, "error": result.error}
