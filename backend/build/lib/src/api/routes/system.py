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

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import update
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
from src.models.user import User
from src.observability.scheduler_registry import get_all_schedulers
from src.observability.vitals import build_system_vitals

router = APIRouter(prefix="/system", tags=["system"])

register_policy("GET", "/api/v1/system/vitals", roles=list(Role))
register_policy("GET", "/api/v1/system/scheduled-jobs", roles=list(Role))
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
