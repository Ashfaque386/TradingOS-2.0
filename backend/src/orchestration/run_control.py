"""Run control (Build Spec §7.3): create / pause / continue / retry
(transient-only) / rerun (always a fresh plan), with race-safe conditional
status transitions via src/orchestration/transitions.py. Cancel is an
addition to the Build Spec's set (backlog item 21, from the old app's
pause/resume/cancel/retry/rerun).
"""

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.organization_run import (
    CANCELLABLE_STATUSES,
    FailureClass,
    OrganizationRun,
    RunSource,
    RunStatus,
    RunType,
)
from src.models.task import Task, TaskStatus
from src.orchestration import events
from src.orchestration.operator_guidance import fold_guidance_into_objective
from src.orchestration.planner import (
    CannotPlanError,
    PlannerFn,
    RunNoLongerPlannableError,
    create_plan,
    fake_llm_planner,
)
from src.orchestration.task_engine import drive_run_to_quiescence
from src.orchestration.transitions import conditional_transition


class PermanentFailureRetryError(Exception):
    """Raised by retry_run() when the run's failure_class is PERMANENT —
    Build Spec §7.3: "Retry only for transient failures"."""


async def create_run(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    *,
    objective: str,
    source: RunSource,
    planner_fn: PlannerFn = fake_llm_planner,
) -> OrganizationRun:
    """Creates a PLANNING run, plans it (up to 3 attempts -> cannot_plan),
    and — if planning succeeded — drives it to quiescence synchronously.
    """
    async with session_factory() as db:
        run = OrganizationRun(
            objective=objective, source=source, run_type=RunType.STANDARD, status=RunStatus.PLANNING
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    async with session_factory() as db:
        await events.emit(
            db, redis, run_id=run_id, event_type="run.created", payload={"objective": objective}
        )

    try:
        async with session_factory() as db:
            # Phase 19 (docs/phase19-audit.md Part 2.6/3.1): every active
            # OperatorGuidance row is folded into the objective the
            # planner actually receives here -- OrganizationRun.objective
            # above stays exactly what the operator typed, so the UI
            # still shows their original text, but the real planning
            # input includes standing guidance.
            planning_objective = await fold_guidance_into_objective(db, objective)
            await create_plan(db, run_id, planning_objective, planner_fn=planner_fn)
    except RunNoLongerPlannableError:
        # Cancelled while being planned: leave the run exactly as the cancel
        # left it -- no tasks, no cannot_plan event, nothing to drive.
        async with session_factory() as db:
            cancelled_run = await db.get(OrganizationRun, run_id)
            assert cancelled_run is not None
            return cancelled_run
    except CannotPlanError:
        async with session_factory() as db:
            await events.emit(db, redis, run_id=run_id, event_type="run.cannot_plan", payload={})
            # Nothing in this codebase ever deletes an OrganizationRun row,
            # and this one was created and committed by this same function
            # a few lines above -- db.get() returning None here isn't a real
            # possibility, only a case db.get()'s own generic signature has
            # to allow for.
            fetched_run = await db.get(OrganizationRun, run_id)
            assert fetched_run is not None
            return fetched_run

    await drive_run_to_quiescence(session_factory, redis, run_id)

    async with session_factory() as db:
        fetched_run = await db.get(OrganizationRun, run_id)
        assert fetched_run is not None
        return fetched_run


async def pause_run(db: AsyncSession, redis: Redis | None, run_id: uuid.UUID) -> bool:
    applied = await conditional_transition(
        db,
        table=OrganizationRun.__table__,
        id_column=OrganizationRun.id,
        row_id=run_id,
        status_column=OrganizationRun.status,
        from_status=RunStatus.RUNNING,
        to_status=RunStatus.PAUSED,
    )
    if applied:
        await events.emit(db, redis, run_id=run_id, event_type="run.paused", payload={})
    return applied


async def continue_run(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, run_id: uuid.UUID
) -> bool:
    async with session_factory() as db:
        applied = await conditional_transition(
            db,
            table=OrganizationRun.__table__,
            id_column=OrganizationRun.id,
            row_id=run_id,
            status_column=OrganizationRun.status,
            from_status=RunStatus.PAUSED,
            to_status=RunStatus.RUNNING,
        )
        if applied:
            await events.emit(db, redis, run_id=run_id, event_type="run.continued", payload={})

    if applied:
        await drive_run_to_quiescence(session_factory, redis, run_id)
    return applied


@dataclass(frozen=True, slots=True)
class CancelResult:
    tasks_cancelled: int
    # Of those, how many were already executing. Their capability runs in a
    # thread that cannot be interrupted, so the work still happens; only its
    # result is discarded.
    tasks_in_flight: int


async def cancel_run(
    db: AsyncSession, redis: Redis | None, run_id: uuid.UUID, *, actor: str
) -> CancelResult | None:
    """Cancels a run from any non-terminal state (including PAUSED). Returns
    None when nothing was cancelled -- the run does not exist or has already
    finished -- so the caller can tell a no-op from a cancel.

    Stops further work; it cannot undo work already done. Finished tasks keep
    their SUCCEEDED/FAILED outcome and artefacts; every unfinished task is
    CANCELLED, including those mid-execution (see `CancelResult`). The run's
    transition is the single atomic step that decides the race against
    pause/continue/another cancel; the task sweep follows it. A crash between
    the two leaves a terminal run with a few unfinished tasks, which is
    harmless: nothing drives a terminal run, and crash recovery only revives
    non-terminal ones.
    """
    applied = await conditional_transition(
        db,
        table=OrganizationRun.__table__,
        id_column=OrganizationRun.id,
        row_id=run_id,
        status_column=OrganizationRun.status,
        from_statuses=CANCELLABLE_STATUSES,
        to_status=RunStatus.CANCELLED,
        extra_values={OrganizationRun.completed_at: func.now()},
    )
    if not applied:
        return None

    # Lock the unfinished tasks, read what state each was in, then cancel them
    # in the same transaction. (An UPDATE ... RETURNING would report the *new*
    # status.) The row locks make the counts exact: a task finishing right now
    # either committed first and drops out of this select, or waits for this
    # commit and then finds itself already CANCELLED -- which is what makes
    # its late result get discarded.
    unfinished = (
        await db.execute(
            select(Task.id, Task.status)
            .where(
                Task.run_id == run_id,
                Task.status.in_(
                    [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING]
                ),
            )
            .with_for_update()
        )
    ).all()
    if unfinished:
        await db.execute(
            update(Task)
            .where(Task.id.in_([task_id for task_id, _ in unfinished]))
            .values(status=TaskStatus.CANCELLED, completed_at=func.now())
        )
    await db.commit()

    result = CancelResult(
        tasks_cancelled=len(unfinished),
        tasks_in_flight=sum(
            1 for _, status in unfinished if status in (TaskStatus.CLAIMED, TaskStatus.RUNNING)
        ),
    )
    await events.emit(
        db,
        redis,
        run_id=run_id,
        event_type="run.cancelled",
        payload={
            "cancelled_by": actor,
            "tasks_cancelled": result.tasks_cancelled,
            "tasks_in_flight": result.tasks_in_flight,
        },
    )
    return result


async def retry_run(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, run_id: uuid.UUID
) -> bool:
    """Only allowed when the run's failure_class is TRANSIENT — raises
    PermanentFailureRetryError otherwise (Build Spec §7.3)."""
    async with session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        if run is None or run.status != RunStatus.FAILED:
            return False
        if run.failure_class != FailureClass.TRANSIENT:
            raise PermanentFailureRetryError(
                f"run {run_id} failed permanently; only a transient failure can be retried"
            )

        # Re-arm every transiently-failed task for reclaim.
        await db.execute(
            update(Task)
            .where(
                Task.run_id == run_id,
                Task.status == TaskStatus.FAILED,
                Task.failure_class == FailureClass.TRANSIENT,
            )
            .values(status=TaskStatus.READY, failure_class=None, last_error=None)
        )
        await db.commit()

        applied = await conditional_transition(
            db,
            table=OrganizationRun.__table__,
            id_column=OrganizationRun.id,
            row_id=run_id,
            status_column=OrganizationRun.status,
            from_status=RunStatus.FAILED,
            to_status=RunStatus.RUNNING,
            extra_values={OrganizationRun.failure_class: None},
        )
        if applied:
            await events.emit(db, redis, run_id=run_id, event_type="run.retried", payload={})

    if applied:
        await drive_run_to_quiescence(session_factory, redis, run_id)
    return applied


async def rerun_run(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    source_run_id: uuid.UUID,
    *,
    planner_fn: PlannerFn = fake_llm_planner,
) -> OrganizationRun | None:
    """Always produces a fresh plan on a brand-new run. The new run's
    run_type is always RunType.RERUN, set explicitly — never copied from
    source_run.run_type, whatever that was (Build Spec §7.3).
    """
    async with session_factory() as db:
        source = await db.get(OrganizationRun, source_run_id)
        if source is None:
            return None

        new_run = OrganizationRun(
            objective=source.objective,
            source=source.source,
            run_type=RunType.RERUN,
            source_run_id=source.id,
            status=RunStatus.PLANNING,
        )
        db.add(new_run)
        await db.commit()
        await db.refresh(new_run)
        new_run_id = new_run.id
        objective = new_run.objective

    async with session_factory() as db:
        await events.emit(
            db,
            redis,
            run_id=new_run_id,
            event_type="run.rerun_created",
            payload={"source_run_id": str(source_run_id)},
        )

    try:
        async with session_factory() as db:
            planning_objective = await fold_guidance_into_objective(db, objective)
            await create_plan(db, new_run_id, planning_objective, planner_fn=planner_fn)
    except (CannotPlanError, RunNoLongerPlannableError):
        async with session_factory() as db:
            return await db.get(OrganizationRun, new_run_id)

    await drive_run_to_quiescence(session_factory, redis, new_run_id)

    async with session_factory() as db:
        return await db.get(OrganizationRun, new_run_id)
