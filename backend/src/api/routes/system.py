"""System Vitals API (Phase 17 real-world testing pass): `GET
/api/v1/system/vitals`, replacing Phase 16 Follow-up E's
Prometheus-Counter-derived `/api/v1/observability/vitals` (since removed)
with a design where each figure is read the way its own semantics
actually require -- see `src.observability.vitals`'s module docstring for
the full reasoning behind the host/llm/latency split.

Same authenticated-read RBAC posture as every other read surface in this
codebase (not `/metrics`'s unauthenticated Prometheus scrape convention).
"""

import secrets
from datetime import UTC, datetime

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import active_llm_provider, llm_provider_health_vitals
from src.agents.scheduler import DEFAULT_HEARTBEAT_INTERVAL_SECONDS
from src.api.schemas import JwtSigningKeyRotateResponse, SystemVitalsResponse
from src.audit.service import write_audit_entry
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.core.security import invalidate_signing_key_cache
from src.gateway.schema import LlmProvider
from src.models.jwt_signing_key import JwtSigningKey
from src.models.refresh_token import RefreshToken
from src.models.scheduled_job_run import ScheduledJobRun
from src.models.user import User
from src.observability.scheduler_registry import get_all_schedulers
from src.observability.vitals import build_system_vitals

router = APIRouter(prefix="/system", tags=["system"])

register_policy("GET", "/api/v1/system/vitals", roles=list(Role))
register_policy("GET", "/api/v1/system/scheduled-jobs", roles=list(Role))
register_policy(
    "GET", "/api/v1/system/scheduled-jobs/{scheduler_name}/{job_id}/history", roles=list(Role)
)
register_policy(
    "POST",
    "/api/v1/system/scheduled-jobs/{scheduler_name}/{job_id}/run-now",
    roles=[Role.SYSTEM_ADMINISTRATOR],
)
register_policy(
    "PUT",
    "/api/v1/system/scheduled-jobs/{scheduler_name}/{job_id}/schedule",
    roles=[Role.SYSTEM_ADMINISTRATOR],
)
register_policy("POST", "/api/v1/system/jwt-signing-key/rotate", roles=[Role.SYSTEM_ADMINISTRATOR])


class ScheduledJobResponse(BaseModel):
    scheduler: str
    job_id: str
    trigger: str
    next_run_time: str | None


class ScheduledJobsResponse(BaseModel):
    jobs: list[ScheduledJobResponse]
    # Heartbeat (src.agents.scheduler.start_heartbeat_loop) is a plain
    # asyncio loop, not an APScheduler job -- it has no next_run_time to
    # report, only a fixed interval, listed separately and honestly
    # rather than forced into the same shape as the real APScheduler jobs
    # above.
    heartbeat_interval_seconds: int


@router.get("/scheduled-jobs")
async def scheduled_jobs_endpoint(
    _current_user: User = Depends(require_role),
) -> ScheduledJobsResponse:
    """Phase 19 (docs/phase19-audit.md Part 1.2): every real scheduled job
    this app runs, with its real live next-run time -- read straight off
    the running APScheduler instances (`src.observability.scheduler_registry`),
    never a hardcoded cadence string."""
    jobs: list[ScheduledJobResponse] = []
    for scheduler_name, scheduler in sorted(get_all_schedulers().items()):
        for job in scheduler.get_jobs():
            jobs.append(
                ScheduledJobResponse(
                    scheduler=scheduler_name,
                    job_id=job.id,
                    trigger=str(job.trigger),
                    next_run_time=job.next_run_time.isoformat() if job.next_run_time else None,
                )
            )
    return ScheduledJobsResponse(
        jobs=jobs, heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    )


class RunScheduledJobNowResponse(BaseModel):
    scheduler: str
    job_id: str
    next_run_time: str | None


class UpdateScheduledJobRequest(BaseModel):
    # Interval jobs (this codebase currently has exactly one:
    # market_data's intraday ingestion) take `seconds`. Cron jobs (every
    # other job) take `hour`/`minute`; any other field of the job's cron
    # expression (day_of_week, etc.) is preserved unchanged from its
    # current trigger rather than exposed here, since no job in this app
    # was registered with a UI to edit those and guessing a replacement
    # would silently change when the job runs.
    seconds: int | None = None
    hour: int | None = None
    minute: int | None = None


class UpdateScheduledJobResponse(BaseModel):
    scheduler: str
    job_id: str
    trigger: str
    next_run_time: str | None


class ScheduledJobRunHistoryEntry(BaseModel):
    scheduled_run_time: str
    finished_at: str
    status: str
    error: str | None


class ScheduledJobHistoryResponse(BaseModel):
    runs: list[ScheduledJobRunHistoryEntry]


def _get_scheduler_and_job(scheduler_name: str, job_id: str) -> tuple[AsyncIOScheduler, Job]:
    scheduler = get_all_schedulers().get(scheduler_name)
    if scheduler is None:
        raise HTTPException(status_code=404, detail=f"No such scheduler: {scheduler_name}")
    job = scheduler.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return scheduler, job


@router.post("/scheduled-jobs/{scheduler_name}/{job_id}/run-now")
async def run_scheduled_job_now_endpoint(
    scheduler_name: str,
    job_id: str,
    current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> RunScheduledJobNowResponse:
    """Fires `job_id` immediately by moving its next firing to now
    (`Job.modify`), the idiomatic APScheduler way to force an out-of-band
    run without disturbing its regular trigger: an interval trigger's
    NEXT next_run_time is computed from this firing, and a cron trigger's
    next match is computed fresh from the cron expression either way, so
    the job's normal cadence is unaffected afterward."""
    scheduler, job = _get_scheduler_and_job(scheduler_name, job_id)
    tz = getattr(job.trigger, "timezone", None) or UTC
    job.modify(next_run_time=datetime.now(tz))
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="scheduled_job.run_now",
        entity_type="scheduled_job",
        entity_id=f"{scheduler_name}/{job_id}",
    )
    return RunScheduledJobNowResponse(
        scheduler=scheduler_name,
        job_id=job_id,
        next_run_time=job.next_run_time.isoformat() if job.next_run_time else None,
    )


@router.put("/scheduled-jobs/{scheduler_name}/{job_id}/schedule")
async def update_scheduled_job_schedule_endpoint(
    scheduler_name: str,
    job_id: str,
    payload: UpdateScheduledJobRequest,
    current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> UpdateScheduledJobResponse:
    """Reschedules a live job in place (`Job.reschedule`). Only the fields
    that make sense for the job's actual trigger type are accepted -- an
    interval job's `seconds`, or a cron job's `hour`/`minute` -- and every
    other field of a cron job's expression (day_of_week, day, month, ...)
    is read off its CURRENT trigger and carried over unchanged, so e.g.
    investor_reporting's `day_of_week="mon"` can't be silently dropped by
    an edit that only meant to change the hour."""
    _scheduler, job = _get_scheduler_and_job(scheduler_name, job_id)
    old_trigger = str(job.trigger)

    if isinstance(job.trigger, IntervalTrigger):
        if payload.hour is not None or payload.minute is not None:
            raise HTTPException(
                status_code=400,
                detail="This job runs on a fixed interval; supply 'seconds', not hour/minute.",
            )
        if payload.seconds is None or payload.seconds < 1:
            raise HTTPException(status_code=400, detail="seconds must be a positive integer")
        new_trigger = IntervalTrigger(seconds=payload.seconds)
    elif isinstance(job.trigger, CronTrigger):
        if payload.seconds is not None:
            raise HTTPException(
                status_code=400,
                detail="This job runs on a cron schedule; supply hour/minute, not seconds.",
            )
        if payload.hour is None or payload.minute is None:
            raise HTTPException(status_code=400, detail="Both hour and minute are required")
        if not (0 <= payload.hour <= 23) or not (0 <= payload.minute <= 59):
            raise HTTPException(status_code=400, detail="hour must be 0-23 and minute must be 0-59")
        cron_kwargs = {field.name: str(field) for field in job.trigger.fields}
        cron_kwargs["hour"] = payload.hour
        cron_kwargs["minute"] = payload.minute
        new_trigger = CronTrigger(**cron_kwargs, timezone=job.trigger.timezone)
    else:
        raise HTTPException(
            status_code=400, detail=f"Unsupported trigger type: {type(job.trigger).__name__}"
        )

    job.reschedule(trigger=new_trigger)
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="scheduled_job.reschedule",
        entity_type="scheduled_job",
        entity_id=f"{scheduler_name}/{job_id}",
        details={"old_trigger": old_trigger, "new_trigger": str(job.trigger)},
    )
    return UpdateScheduledJobResponse(
        scheduler=scheduler_name,
        job_id=job_id,
        trigger=str(job.trigger),
        next_run_time=job.next_run_time.isoformat() if job.next_run_time else None,
    )


@router.get("/scheduled-jobs/{scheduler_name}/{job_id}/history")
async def scheduled_job_history_endpoint(
    scheduler_name: str,
    job_id: str,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> ScheduledJobHistoryResponse:
    """Real past firings of this job, most recent first -- written by
    `src.observability.scheduled_job_history`'s listener as each firing
    completes. Does not require the scheduler/job to currently exist (a
    renamed or removed job's history should still be readable), unlike
    run-now/schedule above which act on the live job."""
    rows = (
        (
            await db.execute(
                select(ScheduledJobRun)
                .where(
                    ScheduledJobRun.scheduler == scheduler_name,
                    ScheduledJobRun.job_id == job_id,
                )
                .order_by(ScheduledJobRun.scheduled_run_time.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return ScheduledJobHistoryResponse(
        runs=[
            ScheduledJobRunHistoryEntry(
                scheduled_run_time=row.scheduled_run_time.isoformat(),
                finished_at=row.finished_at.isoformat(),
                status=row.status.value,
                error=row.error,
            )
            for row in rows
        ]
    )


@router.get("/vitals")
async def system_vitals_endpoint(
    _current_user: User = Depends(require_role),
    redis: Redis = Depends(get_redis),
) -> SystemVitalsResponse:
    settings = get_settings()
    payload = await build_system_vitals(
        redis=redis,
        token_usage_providers=[p.value for p in LlmProvider],
        prometheus_base_url=settings.prometheus_url,
        latency_budget_ms=settings.order_dispatch_latency_budget_ms,
        active_provider=active_llm_provider(),
        provider_health=llm_provider_health_vitals(),
    )
    return SystemVitalsResponse(**payload)


@router.post("/jwt-signing-key/rotate")
async def rotate_jwt_signing_key_endpoint(
    current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> JwtSigningKeyRotateResponse:
    """Phase 21 (docs/phase20-old-vs-new-comparison.md item 11): generates
    a fresh signing key and switches this process to it immediately (see
    `src.core.security`'s `get_current_signing_key`/
    `invalidate_signing_key_cache` for why that no longer needs a
    restart). Deliberately disruptive, matching the operational reality
    this exists for ("the key may be compromised, end every session now"),
    not a routine maintenance action: every access token issued under the
    old key stops validating on this process's very next request (a hard
    cutover, not a grace period -- `decode_token`'s own docstring explains
    why), and every outstanding refresh token is revoked in the same
    transaction so nothing can silently mint a new access token under the
    new key either. The new key value itself is never returned -- same
    write-only posture as every other credential store in this codebase.
    """
    new_key = secrets.token_urlsafe(48)
    now = datetime.now(UTC)

    db.add(JwtSigningKey(key=new_key, created_at=now, created_by=current_user.email))

    revoked = await db.execute(
        update(RefreshToken).where(RefreshToken.revoked_at.is_(None)).values(revoked_at=now)
    )

    await write_audit_entry(
        db,
        actor=current_user.email,
        action="system.jwt_signing_key_rotated",
        entity_type="jwt_signing_key",
        details={"sessions_revoked": revoked.rowcount},
    )
    await db.commit()

    # Only after the new row is durably committed -- if the commit above
    # failed, the next request must keep using the still-valid old key,
    # never a phantom one this process merely thought it wrote.
    invalidate_signing_key_cache()

    return JwtSigningKeyRotateResponse(
        rotated_at=now.isoformat(),
        rotated_by=current_user.email,
        sessions_revoked=revoked.rowcount,
    )
