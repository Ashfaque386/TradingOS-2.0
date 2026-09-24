"""Orchestration engine API routes (Build Spec §7.3, Phase 2 acceptance:
POST an objective, see a task graph get planned and executed against stub
capabilities, pause/resume/retry/rerun it).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.schemas import CreateRunRequest, RunResponse, TaskSummaryResponse
from src.core.db import get_db, get_session_factory
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.models.organization_run import OrganizationRun, RunSource
from src.models.task import Task
from src.models.user import User
from src.orchestration import run_control
from src.orchestration.run_control import PermanentFailureRetryError

router = APIRouter(prefix="/orchestration", tags=["orchestration"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER, Role.RISK_MANAGER]

register_policy(
    "POST", "/api/v1/orchestration/runs", roles=[Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]
)
register_policy("GET", "/api/v1/orchestration/runs", roles=list(Role))
register_policy("GET", "/api/v1/orchestration/runs/{run_id}", roles=list(Role))
register_policy("POST", "/api/v1/orchestration/runs/{run_id}/pause", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/orchestration/runs/{run_id}/continue", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/orchestration/runs/{run_id}/retry", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/orchestration/runs/{run_id}/rerun", roles=_OPERATOR_ROLES)


async def _load_run_response(db: AsyncSession, run_id: uuid.UUID) -> RunResponse:
    run = await db.get(OrganizationRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    tasks_result = await db.execute(
        select(Task).where(Task.run_id == run_id).order_by(Task.created_at)
    )
    tasks = list(tasks_result.scalars())

    return RunResponse(
        id=run.id,
        objective=run.objective,
        source=run.source,
        run_type=run.run_type,
        status=run.status,
        failure_class=run.failure_class,
        source_run_id=run.source_run_id,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        tasks=[TaskSummaryResponse.model_validate(t) for t in tasks],
    )


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run_endpoint(
    body: CreateRunRequest,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    run = await run_control.create_run(
        session_factory, redis, objective=body.objective, source=RunSource.UI
    )
    async with session_factory() as db:
        return await _load_run_response(db, run.id)


@router.get("/runs")
async def list_runs_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RunResponse]:
    run_ids = (
        (await db.execute(select(OrganizationRun.id).order_by(OrganizationRun.created_at.desc())))
        .scalars()
        .all()
    )
    return [await _load_run_response(db, run_id) for run_id in run_ids]


@router.get("/runs/{run_id}")
async def get_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/pause")
async def pause_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    await run_control.pause_run(db, redis, run_id)
    return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/continue")
async def continue_run_endpoint(
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    await run_control.continue_run(session_factory, redis, run_id)
    async with session_factory() as db:
        return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/retry")
async def retry_run_endpoint(
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    try:
        await run_control.retry_run(session_factory, redis, run_id)
    except PermanentFailureRetryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    async with session_factory() as db:
        return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun_run_endpoint(
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    new_run = await run_control.rerun_run(session_factory, redis, run_id)
    if new_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source run not found")
    async with session_factory() as db:
        return await _load_run_response(db, new_run.id)
