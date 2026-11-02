"""Reading, saving and applying the schedules administrators chose for scheduled jobs.

The rules are `src.orchestration.job_schedule`; this module is the database half. A choice is saved
in `scheduled_job_overrides`, and a restart applies each one on top of the schedule the code
registered the job with (which `remember_defaults` recorded first, so it can be restored).
"""

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.scheduled_job_override import ScheduledJobOverride
from src.orchestration.job_schedule import (
    ScheduleRefused,
    ScheduleSpec,
    remember_defaults,
    trigger_for,
)

logger = structlog.get_logger(__name__)


def spec_of(row: ScheduledJobOverride) -> ScheduleSpec:
    return ScheduleSpec(seconds=row.seconds, hour=row.hour, minute=row.minute)


async def overrides_by_job(db: AsyncSession) -> dict[tuple[str, str], ScheduledJobOverride]:
    rows = (await db.execute(select(ScheduledJobOverride))).scalars().all()
    return {(row.scheduler, row.job_id): row for row in rows}


async def save_override(
    db: AsyncSession, scheduler: str, job_id: str, spec: ScheduleSpec, *, updated_by: str
) -> None:
    """Keeps the choice for this job, replacing an earlier one. The caller commits."""
    values: dict[str, Any] = {
        "scheduler": scheduler,
        "job_id": job_id,
        "seconds": spec.seconds,
        "hour": spec.hour,
        "minute": spec.minute,
        "updated_by": updated_by,
    }
    stmt = pg_insert(ScheduledJobOverride).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["scheduler", "job_id"],
            set_={key: values[key] for key in ("seconds", "hour", "minute", "updated_by")}
            | {"updated_at": func.now()},
        )
    )


async def delete_override(db: AsyncSession, scheduler: str, job_id: str) -> bool:
    """Forgets the choice for this job; False if there was none. The caller commits."""
    result = await db.execute(
        delete(ScheduledJobOverride).where(
            ScheduledJobOverride.scheduler == scheduler, ScheduledJobOverride.job_id == job_id
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def apply_stored_overrides(
    session_factory: async_sessionmaker[AsyncSession], schedulers: Mapping[str, Any]
) -> list[str]:
    """Puts every stored choice on its job, after recording the code's own schedules as defaults.
    A choice for a job that no longer exists, or one the job's kind cannot take, is left alone (and
    logged), never allowed to stop the app from starting. Returns what was applied, as sentences."""
    remember_defaults(schedulers)
    applied: list[str] = []
    async with session_factory() as db:
        rows = list((await db.execute(select(ScheduledJobOverride))).scalars().all())
    for row in rows:
        scheduler = schedulers.get(row.scheduler)
        job = scheduler.get_job(row.job_id) if scheduler is not None else None
        if job is None:
            logger.warning(
                "scheduled_job_override.job_gone", scheduler=row.scheduler, job=row.job_id
            )
            continue
        try:
            trigger = trigger_for(job.trigger, spec_of(row))
        except ScheduleRefused as exc:
            logger.warning(
                "scheduled_job_override.not_applied",
                scheduler=row.scheduler,
                job=row.job_id,
                reason=str(exc),
            )
            continue
        job.reschedule(trigger=trigger)
        applied.append(f"{row.scheduler}/{row.job_id}: {trigger}")
    return applied
