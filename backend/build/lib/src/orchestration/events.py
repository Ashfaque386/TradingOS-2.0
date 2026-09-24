"""Event bus (Build Spec §7.3, §5.4): one emit() call does a Postgres insert
+ Redis publish, with a gap-free per-run sequence number.

Gap-free sequencing is enforced by serializing "compute next sequence,
insert" per run_id with a transaction-scoped Postgres advisory lock
(pg_advisory_xact_lock, auto-released at commit/rollback) — concurrent
emit() calls for the same run queue up on the lock rather than racing to
read the same MAX(sequence) and insert a duplicate or skip a number.

The Postgres insert is the durable source of truth and is always
committed; the Redis publish is best-effort. No consumer of these
publishes exists yet in this phase (no websocket subscribers) — coupling
core, DB-backed orchestration progress to Redis's availability would be a
worse failure mode than an occasionally-missed live update, so a publish
failure is logged and swallowed, never raised.
"""

import json
import uuid
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organizational_event import OrganizationalEvent

logger = structlog.get_logger(__name__)


def _lock_key_for_run(run_id: uuid.UUID) -> int:
    # Postgres advisory-lock bigint is signed 64-bit; fold the UUID's 128
    # bits down into that range via a mask (always positive).
    return run_id.int & 0x7FFFFFFFFFFFFFFF


async def emit(
    db: AsyncSession,
    redis: Redis | None,
    *,
    run_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> OrganizationalEvent:
    lock_key = _lock_key_for_run(run_id)
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))

    next_seq = (
        await db.execute(
            select(func.coalesce(func.max(OrganizationalEvent.sequence), 0) + 1).where(
                OrganizationalEvent.run_id == run_id
            )
        )
    ).scalar_one()

    event = OrganizationalEvent(
        run_id=run_id, sequence=next_seq, event_type=event_type, payload=payload
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    if redis is not None:
        try:
            await redis.publish(
                f"organization-events:{run_id}",
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "sequence": event.sequence,
                        "event_type": event.event_type,
                        "payload": payload,
                    }
                ),
            )
        except Exception:
            logger.warning(
                "orchestration.event_publish_failed",
                run_id=str(run_id),
                event_type=event_type,
            )

    return event
