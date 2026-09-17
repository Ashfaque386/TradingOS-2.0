"""Real outbound sends for Telegram/Discord/Slack (Build Spec §18). Each
function makes one real `httpx` call against that channel's actual public
API -- same "fresh AsyncClient per call, injectable transport" shape as
Phase 8's broker adapters (src.brokers.zerodha/upstox), so a caller can
inject `httpx.MockTransport` in tests without touching the real network,
and production code gets a genuine, unmocked HTTP call. This sandbox has
no real bot tokens/webhook URLs configured, so these are exercised here
only against injected transports -- honestly unexercised against the real
Telegram/Discord/Slack APIs, same posture as every other "real code,
no egress in this sandbox" module in this codebase.

Every function returns a `SendResult` rather than raising -- a failed
send (bad token, network error, rate-limited by the channel itself) is a
normal outcome `src.notifications.dispatch.notify` must be able to log
and move on from, not an exception that would take down the rest of the
fan-out.
"""

from dataclasses import dataclass

import httpx
import structlog

from src.notifications.channel_store import NotificationChannelConfig
from src.notifications.types import NotificationChannel

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class SendResult:
    channel: NotificationChannel
    ok: bool
    status_code: int | None
    error: str | None


async def send_telegram(
    config: NotificationChannelConfig,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    if not config.bot_token or not config.chat_id:
        return SendResult(
            NotificationChannel.TELEGRAM, False, None, "bot_token/chat_id not configured"
        )
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(url, json={"chat_id": config.chat_id, "text": text})
        if resp.status_code >= 400:
            return SendResult(
                NotificationChannel.TELEGRAM, False, resp.status_code, resp.text[:500]
            )
        return SendResult(NotificationChannel.TELEGRAM, True, resp.status_code, None)
    except httpx.HTTPError as exc:
        return SendResult(NotificationChannel.TELEGRAM, False, None, str(exc))


async def send_discord(
    config: NotificationChannelConfig,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    if not config.webhook_url:
        return SendResult(NotificationChannel.DISCORD, False, None, "webhook_url not configured")
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(config.webhook_url, json={"content": text[:2000]})
        if resp.status_code >= 400:
            return SendResult(NotificationChannel.DISCORD, False, resp.status_code, resp.text[:500])
        return SendResult(NotificationChannel.DISCORD, True, resp.status_code, None)
    except httpx.HTTPError as exc:
        return SendResult(NotificationChannel.DISCORD, False, None, str(exc))


async def send_slack(
    config: NotificationChannelConfig,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    if not config.webhook_url:
        return SendResult(NotificationChannel.SLACK, False, None, "webhook_url not configured")
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(config.webhook_url, json={"text": text})
        if resp.status_code >= 400:
            return SendResult(NotificationChannel.SLACK, False, resp.status_code, resp.text[:500])
        return SendResult(NotificationChannel.SLACK, True, resp.status_code, None)
    except httpx.HTTPError as exc:
        return SendResult(NotificationChannel.SLACK, False, None, str(exc))


SENDERS = {
    NotificationChannel.TELEGRAM: send_telegram,
    NotificationChannel.DISCORD: send_discord,
    NotificationChannel.SLACK: send_slack,
}


async def send_to_channel(
    channel: NotificationChannel,
    config: NotificationChannelConfig,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    return await SENDERS[channel](config, text, transport=transport)
