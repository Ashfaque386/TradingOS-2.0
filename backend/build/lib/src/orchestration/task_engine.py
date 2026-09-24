"""Task engine (Build Spec §7.3): Postgres-advisory-lock task claiming,
thread-pool dispatch for an allowlisted set of concurrency-safe
capabilities, per-task timeouts, and a 5-minute sweep flagging tasks
stalled >900s.

Capabilities are plain sync callables (src/orchestration/capabilities.py),
dispatched through a shared ThreadPoolExecutor via
`loop.run_in_executor()`. Concurrency-safe capabilities can all be
in-flight at once; every other capability is dispatched one at a time by
the driver loop below.

Concurrent dispatch needs one AsyncSession per in-flight task, never one
shared across `asyncio.gather()` branches — a single SQLAlchemy
AsyncSession is not safe for concurrent use from multiple coroutines
(shared connection/identity-map/transaction state). Every function here
that can fan out therefore takes a `session_factory`
(async_sessionmaker), not a single `db`, and opens its own short-lived
session per unit of work — the same pattern tests use via their own
per-test `db_session_factory` fixture, so this is fully exercisable in
isolation without touching the app's real global engine.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.organization_run import FailureClass, OrganizationRun, RunStatus
from src.models.result_artefact import ResultArtefact
from src.models.task import Task, TaskStatus
from src.observability.correlation import bind_job_correlation_id
from src.orchestration import dependencies, events
from src.orchestration.capabilities import (
    REGISTRY,
    PermanentCapabilityError,
    TransientCapabilityError,
)

logger = structlog.get_logger(__name__)

STALL_THRESHOLD_SECONDS = 900
STALL_SWEEP_INTERVAL_SECONDS = 300

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="task-engine")


def _lock_key_for_task(task_id: uuid.UUID) -> int:
    # Postgres advisory-lock bigint is signed 64-bit; fold the UUID's 128
    # bits down into that range via a mask (always positive).
    return task_id.int & 0x7FFFFFFFFFFFFFFF


async def claim_task(db: AsyncSession, task_id: uuid.UUID, worker_id: str) -> bool:
    """Race-safe claim. A transaction-scoped Postgres advisory lock keyed
    by task_id serializes the check-and-flip against any other worker
    trying to claim the same task concurrently; committing (or rolling
    back) the same transaction releases the lock automatically, so there's
    no separate unlock call and nothing to leak on a crash mid-claim.
    """
    lock_key = _lock_key_for_task(task_id)
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))

    stmt = (
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.READY)
        .values(status=TaskStatus.CLAIMED, claimed_by=worker_id, claimed_at=func.now())
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount == 1


async def _run_capability(fn, params: dict) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, params)


async def _mark_task_failed(
    db: AsyncSession,
    redis: Redis | None,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    message: str,
    transient: bool,
) -> None:
    failure_class = FailureClass.TRANSIENT if transient else FailureClass.PERMANENT
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.FAILED,
            completed_at=func.now(),
            last_error=message,
            failure_class=failure_class,
        )
    )
    await db.commit()
    await events.emit(
        db,
        redis,
        run_id=run_id,
        event_type="task.failed",
        payload={"task_id": str(task_id), "transient": transient, "error": message},
    )
    if not transient:
        await dependencies.propagate_unsatisfiable(db, task_id)


async def execute_claimed_task(db: AsyncSession, redis: Redis | None, task: Task) -> None:
    """Runs one already-CLAIMED task's capability to completion (or
    failure/timeout) using a single session end-to-end — call this with a
    session dedicated to this one task, never shared with a concurrently
    running sibling.
    """
    await db.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(
            status=TaskStatus.RUNNING, started_at=func.now(), attempt_count=Task.attempt_count + 1
        )
    )
    await db.commit()
    await events.emit(
        db,
        redis,
        run_id=task.run_id,
        event_type="task.started",
        payload={"task_id": str(task.id), "capability": task.capability},
    )

    fn = REGISTRY.get(task.capability)
    try:
        result = await asyncio.wait_for(
            _run_capability(fn, task.params), timeout=task.timeout_seconds
        )
    except TimeoutError:
        await _mark_task_failed(
            db, redis, task.id, task.run_id, message="capability timed out", transient=True
        )
        return
    except TransientCapabilityError as exc:
        await _mark_task_failed(db, redis, task.id, task.run_id, message=str(exc), transient=True)
        return
    except PermanentCapabilityError as exc:
        await _mark_task_failed(db, redis, task.id, task.run_id, message=str(exc), transient=False)
        return
    except Exception as exc:  # noqa: BLE001 - unclassified capability errors default to permanent
        await _mark_task_failed(db, redis, task.id, task.run_id, message=str(exc), transient=False)
        return

    await db.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(status=TaskStatus.SUCCEEDED, completed_at=func.now())
    )
    if task.produces_artefact_type and isinstance(result, dict):
        db.add(
            ResultArtefact(
                run_id=task.run_id,
                task_id=task.id,
                artefact_type=task.produces_artefact_type,
                payload=result,
            )
        )
    await db.commit()
    await events.emit(
        db,
        redis,
        run_id=task.run_id,
        event_type="task.succeeded",
        payload={"task_id": str(task.id)},
    )
    await dependencies.resolve_dependencies_on_success(db, task.id)


async def _claim_and_execute_in_own_session(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    task_id: uuid.UUID,
    *,
    worker_id: str,
) -> None:
    async with session_factory() as db:
        if not await claim_task(db, task_id, worker_id):
            return
        task = await db.get(Task, task_id)
        if task is not None:
            await execute_claimed_task(db, redis, task)


async def claim_and_execute_ready_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    run_id: uuid.UUID,
    *,
    worker_id: str,
) -> None:
    """One dispatch batch: claims every currently-READY task for the run
    and executes it — concurrency-safe capabilities all in flight together
    (each in its own session), every other capability claimed+executed one
    at a time. Does not loop; callers needing to drive a run to quiescence
    call this repeatedly (drive_run_to_quiescence below), since resolving
    one batch's dependencies can make new tasks READY.
    """
    async with session_factory() as db:
        ready_result = await db.execute(
            select(Task).where(Task.run_id == run_id, Task.status == TaskStatus.READY)
        )
        ready_tasks = list(ready_result.scalars())

    concurrent_ids = [t.id for t in ready_tasks if REGISTRY.is_concurrency_safe(t.capability)]
    serial_ids = [t.id for t in ready_tasks if not REGISTRY.is_concurrency_safe(t.capability)]

    if concurrent_ids:
        await asyncio.gather(
            *(
                _claim_and_execute_in_own_session(
                    session_factory, redis, task_id, worker_id=worker_id
                )
                for task_id in concurrent_ids
            )
        )

    for task_id in serial_ids:
        await _claim_and_execute_in_own_session(
            session_factory, redis, task_id, worker_id=worker_id
        )


async def run_has_pending_work(db: AsyncSession, run_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(Task.id)
        .where(
            Task.run_id == run_id,
            Task.status.in_(
                [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING]
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def determine_run_outcome(
    db: AsyncSession, run_id: uuid.UUID
) -> tuple[RunStatus, FailureClass | None]:
    """Called once a run has no more pending/ready/claimed/running tasks.
    FAILED+PERMANENT if any task permanently failed or was left
    unsatisfiable; FAILED+TRANSIENT if the only failures were transient
    (retry_run() can act on those); COMPLETED otherwise.
    """
    failure_classes_result = await db.execute(
        select(Task.failure_class).where(Task.run_id == run_id, Task.status == TaskStatus.FAILED)
    )
    failure_classes = {row for (row,) in failure_classes_result.all() if row is not None}

    unsatisfiable_result = await db.execute(
        select(Task.id)
        .where(Task.run_id == run_id, Task.status == TaskStatus.UNSATISFIABLE)
        .limit(1)
    )
    has_unsatisfiable = unsatisfiable_result.scalar_one_or_none() is not None

    if FailureClass.PERMANENT in failure_classes or has_unsatisfiable:
        return RunStatus.FAILED, FailureClass.PERMANENT
    if FailureClass.TRANSIENT in failure_classes:
        return RunStatus.FAILED, FailureClass.TRANSIENT
    return RunStatus.COMPLETED, None


async def drive_run_to_quiescence(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    run_id: uuid.UUID,
    *,
    worker_id: str = "inline",
) -> RunStatus:
    """Repeatedly dispatches ready-task batches until the run has no more
    pending/ready/claimed/running work, then records the final outcome.
    Used both by the API's synchronous "plan and execute" path and by
    crash recovery resuming a reaped run. Stops early (without recording an
    outcome) if the run leaves RUNNING mid-flight — e.g. a concurrent pause.
    """
    while True:
        async with session_factory() as db:
            run = await db.get(OrganizationRun, run_id)
            if run is None or run.status != RunStatus.RUNNING:
                return run.status if run is not None else RunStatus.FAILED
            has_work = await run_has_pending_work(db, run_id)
        if not has_work:
            break
        await claim_and_execute_ready_tasks(session_factory, redis, run_id, worker_id=worker_id)

    async with session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        if run is None or run.status != RunStatus.RUNNING:
            return run.status if run is not None else RunStatus.FAILED

        final_status, failure_class = await determine_run_outcome(db, run_id)
        await db.execute(
            update(OrganizationRun)
            .where(OrganizationRun.id == run_id, OrganizationRun.status == RunStatus.RUNNING)
            .values(status=final_status, failure_class=failure_class, completed_at=func.now())
        )
        await db.commit()
        await events.emit(
            db,
            redis,
            run_id=run_id,
            event_type="run.completed",
            payload={"status": final_status.value},
        )
        return final_status


async def run_stall_sweep_once(db: AsyncSession, redis: Redis | None) -> list[uuid.UUID]:
    """Flags every RUNNING task whose started_at is older than
    STALL_THRESHOLD_SECONDS and hasn't already been flagged. Returns the
    flagged task ids. Intended to be called every STALL_SWEEP_INTERVAL_SECONDS
    by the background loop below.
    """
    threshold = datetime.now(UTC) - timedelta(seconds=STALL_THRESHOLD_SECONDS)
    result = await db.execute(
        select(Task).where(
            Task.status == TaskStatus.RUNNING,
            Task.started_at.is_not(None),
            Task.started_at < threshold,
            Task.stalled_flagged_at.is_(None),
        )
    )
    stalled_tasks = list(result.scalars())

    for task in stalled_tasks:
        await db.execute(
            update(Task).where(Task.id == task.id).values(stalled_flagged_at=func.now())
        )
        await db.commit()
        await events.emit(
            db,
            redis,
            run_id=task.run_id,
            event_type="task.stalled",
            payload={"task_id": str(task.id), "capability": task.capability},
        )

    return [t.id for t in stalled_tasks]


def start_stall_sweep_loop(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None
) -> asyncio.Task:
    async def _loop() -> None:
        while True:
            await asyncio.sleep(STALL_SWEEP_INTERVAL_SECONDS)
            async with bind_job_correlation_id("stall_sweep"):
                try:
                    async with session_factory() as db:
                        await run_stall_sweep_once(db, redis)
                except Exception:  # noqa: BLE001 - a sweep failure must never kill the loop
                    logger.exception("orchestration.stall_sweep_failed")

    return asyncio.create_task(_loop())
