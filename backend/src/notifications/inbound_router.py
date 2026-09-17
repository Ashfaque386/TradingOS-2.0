"""Routes a verified inbound channel message to the CEO Agent (Build Spec
§18) -- the one function every inbound webhook route
(src.api.routes.webhooks) calls after signature verification, replay-
guard, and rate-limit have all passed. Two outcomes, chosen by
src.notifications.classify.classify_message:

1. **Objective** -- spawns a real, persisted organization run via
   src.orchestration.run_control.create_run(source=RunSource.WEBHOOK),
   the same Phase 2 orchestration engine Build Spec §7.3 built (not a
   second, parallel "chat-triggered run" mechanism) -- `RunSource.WEBHOOK`
   has existed on the enum since Phase 2 specifically for this call site.
2. **Query** -- a direct, synchronous CEO Agent reply via the LLM router
   (src.agents.llm_router), falling back to a deterministic canned
   response when every provider is exhausted -- the exact same
   `LlmRouterExhaustedError` -> fallback shape src.agents.graph's
   `_llm_or_fallback` already established, not a second fallback
   convention.

Either way, the reply is sent back out through the *same* channel the
message arrived on (src.notifications.senders) -- a sender never has to
poll anywhere else to see the system's response.

**Deny-by-default on the sender allowlist.** A message from a sender_id
not in the channel's `allowed_sender_ids` is never routed anywhere --
logged and dropped, not silently accepted as anonymous. This is the
"verified sender" half of Build Spec §18's routing requirement; signature
verification proves the *channel* is genuine, this proves the *specific
human* is allowed to command the system through it.
"""

from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.models.organization_run import RunSource
from src.notifications.channel_store import NotificationChannelStore
from src.notifications.classify import classify_message
from src.notifications.senders import SendResult, send_to_channel
from src.notifications.types import NotificationChannel
from src.orchestration.run_control import create_run

logger = structlog.get_logger(__name__)

_CEO_AGENT_ID = "ceo-agent"
_FALLBACK_QUERY_REPLY = (
    "TradingOS received your message but no LLM provider is currently configured -- "
    "here is what I can tell you without one: send an imperative instruction "
    "(e.g. 'research NIFTY momentum strategies') to spawn a real organization run, "
    "or ask again once a provider is configured for a real answer."
)


@dataclass(frozen=True, slots=True)
class InboundRouteResult:
    accepted: bool
    reason: str | None
    classification: str | None
    run_id: str | None
    reply_text: str | None
    send_result: SendResult | None


async def route_inbound_message(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    channel_store: NotificationChannelStore,
    *,
    channel: NotificationChannel,
    sender_id: str,
    text: str,
    llm_router: LlmRouter | None = None,
) -> InboundRouteResult:
    config = channel_store.get_config(channel)
    if config is None or not config.enabled:
        logger.info("notifications.inbound_channel_not_configured", channel=channel.value)
        return InboundRouteResult(False, "channel not configured", None, None, None, None)

    if sender_id not in config.allowed_sender_ids:
        logger.warning(
            "notifications.inbound_sender_not_verified", channel=channel.value, sender_id=sender_id
        )
        return InboundRouteResult(False, "sender not in verified allowlist", None, None, None, None)

    classification = classify_message(text)

    if classification == "objective":
        run = await create_run(session_factory, redis, objective=text, source=RunSource.WEBHOOK)
        reply_text = f"Started organization run {run.id} for: {text}"
        run_id = str(run.id)
    else:
        router = llm_router if llm_router is not None else get_llm_router()
        try:
            result = await router.complete(agent_id=_CEO_AGENT_ID, prompt=text)
            reply_text = result.text
        except LlmRouterExhaustedError:
            logger.warning("notifications.ceo_agent_reply_using_fallback", channel=channel.value)
            reply_text = _FALLBACK_QUERY_REPLY
        run_id = None

    send_result = await send_to_channel(channel, config, reply_text)

    return InboundRouteResult(
        accepted=True,
        reason=None,
        classification=classification,
        run_id=run_id,
        reply_text=reply_text,
        send_result=send_result,
    )
