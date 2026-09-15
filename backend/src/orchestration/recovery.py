"""Crash recovery (Build Spec §7.3): on every boot, re-enter any run left
non-terminal by a prior crash.

A run in PENDING/PLANNING/RUNNING the instant the app starts can only be
there because a previous process died mid-flight — nothing else could
still be legitimately in progress at that exact moment. Any task that was
CLAIMED or RUNNING at crash time is reset to READY (its claimer can't
still be alive), so the normal dispatch loop can reclaim and re-run it.
PAUSED runs are left untouched: pause is a deliberate human decision, not
a crash artefact, and reaping must never override it.
"""

import uuid

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organization_run import NON_TERMINAL_STATUSES, OrganizationRun
from src.models.task import Task, TaskStatus
from src.orchestration import events


async def reap_incomplete_runs(db: AsyncSession, redis: Redis | None) -> list[uuid.UUID]:
    result = await db.execute(
        select(OrganizationRun.id).where(OrganizationRun.status.in_(NON_TERMINAL_STATUSES))
    )
    run_ids = [row for (row,) in result.all()]
    if not run_ids:
        return []

    await db.execute(
        update(Task)
        .where(Task.run_id.in_(run_ids), Task.status.in_([TaskStatus.CLAIMED, TaskStatus.RUNNING]))
        .values(status=TaskStatus.READY, claimed_by=None, claimed_at=None, started_at=None)
    )
    await db.commit()

    for run_id in run_ids:
        await events.emit(db, redis, run_id=run_id, event_type="run.reaped", payload={})

    return run_ids
