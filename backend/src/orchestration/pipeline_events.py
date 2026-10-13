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

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.agent_pipeline_event import AgentPipelineEvent

logger = structlog.get_logger(__name__)

PIPELINE_EVENTS_CHANNEL_PREFIX = "agent-pipeline-events"

# Cancelling a run is a Redis flag keyed by run id (the same shape as the
# chat stream's abort flag, src.orchestration.chat): the pipeline runs inside
# the HTTP request that started it, so the cancel arrives on a different
# request and has to reach the running one through shared state. The TTL only
# bounds a flag nobody consumes (a run that died); a live run clears it on
# the way out.
CANCEL_KEY_PREFIX = "agents:pipeline:cancel"
CANCEL_FLAG_TTL_SECONDS = 3600

# The event types that end a run -- once one exists the run is over and a
# cancel has nothing left to stop. `pipeline.abandoned` is written by the reaper
# (below) for a run whose process died: it leaves no ending of its own.
ABANDONED_EVENT_TYPE = "pipeline.abandoned"
TERMINAL_EVENT_TYPES = (
    "pipeline.completed",
    "pipeline.failed",
    "pipeline.cancelled",
    ABANDONED_EVENT_TYPE,
)

# A run executes inside the HTTP request that started it, so when that process
# dies (a crash, a redeploy, an OOM kill) nothing is left to say so -- the
# event log just stops, and the run read "running" for ever, with a cancel that
# would be accepted and never consumed. Liveness is a Redis key the running
# process refreshes while the run executes; if it stops being refreshed it
# expires within ALIVE_TTL_SECONDS. A run with no terminal event and no live
# key, and no new event for ABANDONED_AFTER_SECONDS, is abandoned.
ALIVE_KEY_PREFIX = "agents:pipeline:alive"
ALIVE_INTERVAL_SECONDS = 10.0
ALIVE_TTL_SECONDS = 30
ABANDONED_AFTER_SECONDS = 60.0
# With no Redis there is no liveness signal at all, so the only evidence is
# silence -- and an LLM call can legitimately take minutes -- so wait far longer.
ABANDONED_AFTER_SECONDS_WITHOUT_LIVENESS = 600.0
REAP_INTERVAL_SECONDS = 60.0


def channel_for(run_id: uuid.UUID | str) -> str:
    return f"{PIPELINE_EVENTS_CHANNEL_PREFIX}:{run_id}"


def cancel_key_for(run_id: uuid.UUID | str) -> str:
    return f"{CANCEL_KEY_PREFIX}:{run_id}"


def alive_key_for(run_id: uuid.UUID | str) -> str:
    return f"{ALIVE_KEY_PREFIX}:{run_id}"


async def request_cancel(redis: Redis, run_id: uuid.UUID | str) -> None:
    await redis.set(cancel_key_for(run_id), "1", ex=CANCEL_FLAG_TTL_SECONDS)


async def clear_cancel(redis: Redis, run_id: uuid.UUID | str) -> None:
    await redis.delete(cancel_key_for(run_id))


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
        *,
        start_sequence: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self.run_id = run_id
        # Non-zero only for the reaper, which continues a dead run's log.
        self._sequence = start_sequence

    @asynccontextmanager
    async def heartbeat(self) -> AsyncIterator[None]:
        """Say "this run's process is alive" for as long as the block runs. The
        key is set before the block starts (so it exists before the run's first
        event), refreshed every ALIVE_INTERVAL_SECONDS, and removed on the way
        out; if the process dies instead, it simply expires. No Redis, no signal
        -- the run proceeds as it always did."""
        redis = self._redis
        if redis is None:
            yield
            return
        key = alive_key_for(self.run_id)

        async def _set() -> None:
            try:
                await redis.set(key, "1", ex=ALIVE_TTL_SECONDS)
            except Exception:  # noqa: BLE001 - liveness is advisory; never fail the run
                logger.warning("pipeline_events.heartbeat_failed", run_id=str(self.run_id))

        async def _beat() -> None:
            while True:
                await asyncio.sleep(ALIVE_INTERVAL_SECONDS)
                await _set()

        await _set()
        beat = asyncio.create_task(_beat())
        try:
            yield
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            try:
                await redis.delete(key)
            except Exception:  # noqa: BLE001 - it expires on its own
                logger.warning("pipeline_events.heartbeat_not_cleared", run_id=str(self.run_id))

    async def is_cancelled(self) -> bool:
        """`CancelSignal` for `src.agents.graph`: has an operator asked for
        this run to stop? Reads the flag fresh each time (the whole point is
        that another request set it). No Redis means no cancel channel at all,
        so the answer is simply 'no'."""
        if self._redis is None:
            return False
        return bool(await self._redis.exists(cancel_key_for(self.run_id)))

    async def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.emit_checked(event_type, node=node, agent_id=agent_id, payload=payload)

    async def emit_checked(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """`emit`, reporting whether the durable row was written -- which the
        reaper needs (it must not claim to have ended a run it did not)."""
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
        persisted = True
        try:
            async with self._session_factory() as db:
                db.add(event)
                await db.commit()
        except Exception:  # noqa: BLE001 - observability must never break the run
            persisted = False
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
        return persisted


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


PipelineRunState = Literal["unknown", "running", "abandoned", "finished"]


async def _process_is_gone(
    redis: Redis | None, run_id: uuid.UUID, last_event_at: datetime, now: datetime
) -> bool:
    """No terminal event exists. Is the process that was running this gone?"""
    silence = (now - last_event_at).total_seconds()
    if redis is not None:
        try:
            if await redis.exists(alive_key_for(run_id)):
                return False
            return silence > ABANDONED_AFTER_SECONDS
        except Exception:  # noqa: BLE001 - fall back to the weaker evidence
            logger.warning("pipeline_events.liveness_unreadable", run_id=str(run_id))
    return silence > ABANDONED_AFTER_SECONDS_WITHOUT_LIVENESS


async def pipeline_run_state(
    db: AsyncSession,
    run_id: uuid.UUID,
    redis: Redis | None = None,
    *,
    now: datetime | None = None,
) -> PipelineRunState:
    """`unknown` (no events recorded under this id), `finished` (a terminal
    event exists), `abandoned` (no terminal event and the process that was
    running it is gone -- see the liveness note above) or `running`. Derived
    from the durable event log, the same source the feed replays, plus the
    liveness key when Redis is available."""
    total, terminal, last_at = (
        await db.execute(
            select(
                func.count(AgentPipelineEvent.id),
                func.count(AgentPipelineEvent.id).filter(
                    AgentPipelineEvent.event_type.in_(TERMINAL_EVENT_TYPES)
                ),
                func.max(AgentPipelineEvent.created_at),
            ).where(AgentPipelineEvent.run_id == run_id)
        )
    ).one()
    if total == 0:
        return "unknown"
    if terminal > 0:
        return "finished"
    if await _process_is_gone(redis, run_id, last_at, now or datetime.now(UTC)):
        return "abandoned"
    return "running"


async def reap_abandoned_runs(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Give every abandoned run the ending it could not write itself: a
    `pipeline.abandoned` event, persisted and published like any other, so the
    feed stops showing a spinner for a run that will never finish. A run with a
    live heartbeat -- including one on another instance -- is left alone, and a
    run that already has a terminal event is never touched, so this is safe to
    run on every start and every minute. Returns the runs it ended."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=ABANDONED_AFTER_SECONDS)
    async with session_factory() as db:
        candidates = (
            await db.execute(
                select(AgentPipelineEvent.run_id, func.max(AgentPipelineEvent.sequence))
                .group_by(AgentPipelineEvent.run_id)
                .having(func.max(AgentPipelineEvent.created_at) < cutoff)
                .having(
                    func.count(AgentPipelineEvent.id).filter(
                        AgentPipelineEvent.event_type.in_(TERMINAL_EVENT_TYPES)
                    )
                    == 0
                )
            )
        ).all()

    reaped: list[uuid.UUID] = []
    for run_id, last_sequence in candidates:
        async with session_factory() as db:
            events = await list_pipeline_events(db, run_id=run_id, limit=10_000)
        last = events[-1]
        if not await _process_is_gone(redis, run_id, last.created_at, now):
            continue
        recorder = PipelineEventRecorder(
            session_factory, redis, run_id, start_sequence=int(last_sequence)
        )
        written = await recorder.emit_checked(
            ABANDONED_EVENT_TYPE,
            node=last.node,
            payload={
                "reason": "the process running this pipeline ended before the run finished",
                "last_event": last.event_type,
                "last_event_at": last.created_at.isoformat(),
                "steps_completed": sum(1 for e in events if e.event_type == "step.completed"),
            },
        )
        if written:
            reaped.append(run_id)
    if reaped:
        logger.warning("pipeline_events.abandoned_runs_ended", run_ids=[str(r) for r in reaped])
    return reaped


def start_abandoned_run_reaper_loop(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None
) -> asyncio.Task[None]:
    async def _loop() -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_SECONDS)
            try:
                await reap_abandoned_runs(session_factory, redis)
            except Exception:  # noqa: BLE001 - a sweep failure must never kill the loop
                logger.exception("pipeline_events.reap_failed")

    return asyncio.create_task(_loop())
