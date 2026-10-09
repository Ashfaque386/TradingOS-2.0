"""Live per-step agent feed (Phase 19 Finding #1): the `PipelineEventSink`
(`src.agents.graph`) that persists each step of a LangGraph pipeline run and
publishes it for any live subscriber.

Why this isn't `src.orchestration.events.emit`: that writes
`organizational_events`, whose `run_id` is a foreign key to
`organization_runs` (the Phase 2 task-graph engine). The LangGraph pipeline
has never been an organization run, and forcing it to become one would put
phantom runs on the Mission Control Kanban -- see `AgentPipelineEvent`.

Each event commits in its own short-lived session rather than riding the
request's session: a pipeline run holds an LLM call open for seconds, and
the feed has to show a step the moment it starts, not when the request's
transaction eventually commits.

What this deliberately does not do is stream LLM tokens. `LlmRouter.
stream_complete` streams for real only for Anthropic; every other provider
is a finished response re-chopped into word chunks, so a "live reasoning"
view built on it would be an invented typing effect on most deployments.
Each step instead reports what is actually known: when it started, which
provider answered (or that the deterministic fallback did), how long it
took, and a bounded summary of what it produced.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.agent_pipeline_event import AgentPipelineEvent

logger = structlog.get_logger(__name__)

PIPELINE_EVENTS_CHANNEL_PREFIX = "agent-pipeline-events"


def channel_for(run_id: uuid.UUID | str) -> str:
    return f"{PIPELINE_EVENTS_CHANNEL_PREFIX}:{run_id}"


def event_to_dict(event: AgentPipelineEvent) -> dict[str, Any]:
    return {
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "node": event.node,
        "agent_id": event.agent_id,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


class PipelineEventRecorder:
    """One instance per pipeline run. `sequence` is a plain counter, not a
    locked `MAX(sequence) + 1`: a run executes its nodes one after another
    in a single process, so there is exactly one writer per `run_id`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis | None,
        run_id: uuid.UUID,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self.run_id = run_id
        self._sequence = 0

    async def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._sequence += 1
        event = AgentPipelineEvent(
            run_id=self.run_id,
            sequence=self._sequence,
            event_type=event_type,
            node=node,
            agent_id=agent_id,
            payload=payload,
            created_at=datetime.now(UTC),
        )

        # Persist and publish independently: losing the durable row must not
        # also blank the live feed, and a Redis outage must not lose the row.
        try:
            async with self._session_factory() as db:
                db.add(event)
                await db.commit()
        except Exception:  # noqa: BLE001 - observability must never break the run
            logger.warning(
                "pipeline_events.persist_failed",
                run_id=str(self.run_id),
                event_type=event_type,
                node=node,
            )

        if self._redis is not None:
            try:
                await self._redis.publish(
                    channel_for(self.run_id), json.dumps(event_to_dict(event))
                )
            except Exception:  # noqa: BLE001 - best-effort, same posture as events.emit
                logger.warning(
                    "pipeline_events.publish_failed",
                    run_id=str(self.run_id),
                    event_type=event_type,
                )


async def list_pipeline_events(
    db: AsyncSession, *, run_id: uuid.UUID | None = None, limit: int = 100
) -> list[AgentPipelineEvent]:
    """Chronological events for one run, or -- with no `run_id` -- the most
    recent `limit` events across all runs (what a feed panel backfills from
    when it first opens)."""
    if run_id is not None:
        result = await db.execute(
            select(AgentPipelineEvent)
            .where(AgentPipelineEvent.run_id == run_id)
            .order_by(AgentPipelineEvent.sequence)
            .limit(limit)
        )
        return list(result.scalars().all())

    result = await db.execute(
        select(AgentPipelineEvent)
        .order_by(AgentPipelineEvent.created_at.desc(), AgentPipelineEvent.sequence.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))
