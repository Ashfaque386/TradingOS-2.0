"""Orchestration engine API routes (Build Spec §7.3, Phase 2 acceptance:
POST an objective, see a task graph get planned and executed against stub
capabilities, pause/resume/retry/rerun it).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.paging import set_total_count
from src.api.schemas import CreateRunRequest, RunResponse, TaskSummaryResponse
from src.core.db import get_db, get_session_factory
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.models.organization_run import (
    TERMINAL_STATUSES,
    OrganizationRun,
    RunSource,
    RunStatus,
)
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
register_policy("POST", "/api/v1/orchestration/runs/{run_id}/cancel", roles=_OPERATOR_ROLES)


async def _load_run_response(db: AsyncSession, run_id: uuid.UUID) -> RunResponse:
    run = await db.get(OrganizationRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    tasks_result = await db.execute(
        select(Task).where(Task.run_id == run_id).order_by(Task.created_at)
    )
    return _run_response(run, list(tasks_result.scalars()))


def _run_response(run: OrganizationRun, tasks: list[Task]) -> RunResponse:
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


def _no_op_refusal(run: OrganizationRun | None, *, verb: str, applies_to: str) -> HTTPException:
    """The refusal for a pause/continue/retry that changed nothing. It is not
    answered 200: the audit middleware writes a row for every 2xx, so a 200
    no-op would put a "paused"/"continued"/"retried" entry in the permanent
    trail for something that never happened (cancel is refused for the same
    reason). 404 for a run that does not exist, otherwise 409 naming the run's
    real status."""
    if run is None:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"This run is {run.status.value}; only {applies_to} can be {verb}",
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
    response: Response,
    limit: int = Query(default=50, gt=0, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: RunStatus | None = None,
    active: bool = False,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RunResponse]:
    """One page of runs, newest first, each with its tasks; `X-Total-Count` is how many runs the
    filters match in all. `active` keeps only the runs not yet finished (pending, planning, running
    or paused), however old: what a board of open work and a count of live runs need, and what a
    page of the newest runs would lose behind newer finished ones.

    The tasks of the whole page are read in one query: the list used to read each run and then its
    tasks, two queries per run, for every run there had ever been."""
    conditions = []
    if status_filter is not None:
        conditions.append(OrganizationRun.status == status_filter)
    if active:
        conditions.append(OrganizationRun.status.notin_(TERMINAL_STATUSES))
    total = (
        await db.execute(select(func.count(OrganizationRun.id)).where(*conditions))
    ).scalar_one()
    set_total_count(response, total)
    runs = list(
        (
            await db.execute(
                select(OrganizationRun)
                .where(*conditions)
                .order_by(OrganizationRun.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    tasks_by_run: dict[uuid.UUID, list[Task]] = {run.id: [] for run in runs}
    if runs:
        for task in (
            await db.execute(
                select(Task).where(Task.run_id.in_(tasks_by_run)).order_by(Task.created_at)
            )
        ).scalars():
            tasks_by_run[task.run_id].append(task)
    return [_run_response(run, tasks_by_run[run.id]) for run in runs]


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
    if not await run_control.pause_run(db, redis, run_id):
        raise _no_op_refusal(
            await db.get(OrganizationRun, run_id), verb="paused", applies_to="a running run"
        )
    return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(require_role),
) -> RunResponse:
    """Stops a pending/planning/running/paused run for good (a cancelled run
    is terminal; Rerun starts a fresh one). 409 -- not pause's 200 no-op --
    when there was nothing to cancel: the audit middleware writes a row for
    every 2xx, and a "cancel" row for a run that was already finished would
    put something in the permanent trail that never happened."""
    result = await run_control.cancel_run(db, redis, run_id, actor=current_user.email)
    if result is None:
        run = await db.get(OrganizationRun, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This run is {run.status.value}; only a pending, planning, running or paused "
                "run can be cancelled"
            ),
        )
    return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/continue")
async def continue_run_endpoint(
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    applied = await run_control.continue_run(session_factory, redis, run_id)
    async with session_factory() as db:
        if not applied:
            raise _no_op_refusal(
                await db.get(OrganizationRun, run_id), verb="continued", applies_to="a paused run"
            )
        return await _load_run_response(db, run_id)


@router.post("/runs/{run_id}/retry")
async def retry_run_endpoint(
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> RunResponse:
    try:
        applied = await run_control.retry_run(session_factory, redis, run_id)
    except PermanentFailureRetryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    async with session_factory() as db:
        if not applied:
            raise _no_op_refusal(
                await db.get(OrganizationRun, run_id),
                verb="retried",
                applies_to="a failed run",
            )
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
