"""The single outbound-alert choke point (Build Spec §18): every one of
this phase's four alert levels (kill-switch trip, sign-off item, go-live
gate pass, daily summary -- src.notifications.types.AlertLevel, the exact
set app/settings/page.tsx's ALERT_LEVELS already names) fans out through
`notify()`, never through a caller constructing its own httpx call. A
channel with no config, disabled, or not subscribed to this alert level
is silently skipped -- that is the normal, expected case for a fresh
install with nothing connected yet, not an error.

**Never raises.** A caller (the kill switch, the approvals module, the
live-trading pipeline) must never have its own transaction/flow broken by
a notification failure -- every per-channel send is wrapped, logged, and
folded into the returned result list rather than propagated. This is the
same "best-effort side channel" posture Phase 2's event bus already
established for its own Redis publish.
"""

import structlog

from src.notifications.channel_store import get_notification_channel_store
from src.notifications.senders import SendResult, send_to_channel
from src.notifications.types import AlertLevel

logger = structlog.get_logger(__name__)


async def notify(
    level: AlertLevel, *, title: str, body: str, details: dict | None = None
) -> list[SendResult]:
    text = f"[{level.value.upper()}] {title}\n{body}"
    if details:
        text += "\n" + "\n".join(f"{k}: {v}" for k, v in details.items())

    try:
        store = get_notification_channel_store()
    except Exception:  # noqa: BLE001 - no encryption key configured yet is a valid, silent no-op
        logger.warning("notifications.store_unavailable", level=level.value)
        return []

    channels = store.channels_subscribed_to(level)
    results: list[SendResult] = []
    for channel in channels:
        config = store.get_config(channel)
        if config is None:
            continue
        try:
            result = await send_to_channel(channel, config, text)
        except Exception as exc:  # noqa: BLE001 - one channel's failure must never block another
            logger.exception("notifications.send_crashed", channel=channel.value, level=level.value)
            result = SendResult(channel, False, None, str(exc))
        results.append(result)
        if result.ok:
            logger.info("notifications.alert_sent", channel=channel.value, level=level.value)
        else:
            logger.warning(
                "notifications.alert_failed",
                channel=channel.value,
                level=level.value,
                error=result.error,
            )

    return results
