import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus
from src.orchestration.task_engine import (
    STALL_THRESHOLD_SECONDS,
    claim_and_execute_ready_tasks,
    claim_task,
    determine_run_outcome,
    run_stall_sweep_once,
)


async def _make_run_and_plan(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        await db.flush()
        plan = OrganizationalPlan(
            run_id=run.id, attempt_number=1, status=PlanStatus.ACCEPTED, objective="obj"
        )
        db.add(plan)
        await db.commit()
        return run.id, plan.id


async def _add_task(
    db_session_factory,
    run_id: uuid.UUID,
    plan_id: uuid.UUID,
    *,
    plan_key: str,
    capability: str,
    status: TaskStatus = TaskStatus.READY,
    params: dict | None = None,
    started_at: datetime | None = None,
) -> uuid.UUID:
    async with db_session_factory() as db:
        task = Task(
            run_id=run_id,
            plan_id=plan_id,
            plan_key=plan_key,
            capability=capability,
            name=plan_key,
            params=params or {},
            status=status,
            started_at=started_at,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task.id


async def test_claim_task_two_workers_only_one_wins(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    task_id = await _add_task(
        db_session_factory, run_id, plan_id, plan_key="t1", capability="stub_noop_concurrent"
    )

    async def _attempt(worker_id: str) -> bool:
        async with db_session_factory() as db:
            return await claim_task(db, task_id, worker_id)

    results = await asyncio.gather(_attempt("worker-a"), _attempt("worker-b"))
    assert sorted(results) == [False, True]

    async with db_session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == TaskStatus.CLAIMED
        assert task.claimed_by in ("worker-a", "worker-b")


async def test_concurrency_safe_capabilities_run_in_parallel(db_session_factory, redis_client):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    for i in range(2):
        await _add_task(
            db_session_factory,
            run_id,
            plan_id,
            plan_key=f"slow-{i}",
            capability="stub_slow_concurrent",
            params={"sleep_seconds": 0.3},
        )

    started = time.monotonic()
    await claim_and_execute_ready_tasks(db_session_factory, redis_client, run_id, worker_id="w")
    elapsed = time.monotonic() - started

    # Two 0.3s tasks run sequentially would take >=0.6s; run concurrently,
    # comfortably under that.
    assert elapsed < 0.55

    async with db_session_factory() as db:
        result = await db.execute(select(Task.status).where(Task.run_id == run_id))
        statuses = [s for (s,) in result.all()]
        assert statuses == [TaskStatus.SUCCEEDED, TaskStatus.SUCCEEDED]


async def test_non_concurrency_safe_capabilities_run_one_at_a_time(
    db_session_factory, redis_client
):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    for i in range(2):
        await _add_task(
            db_session_factory,
            run_id,
            plan_id,
            plan_key=f"slow-serial-{i}",
            capability="stub_slow_serial",
            params={"sleep_seconds": 0.3},
        )

    started = time.monotonic()
    await claim_and_execute_ready_tasks(db_session_factory, redis_client, run_id, worker_id="w")
    elapsed = time.monotonic() - started

    # Dispatched one at a time: two 0.3s tasks take close to 0.6s, not ~0.3s.
    assert elapsed >= 0.55


async def test_determine_run_outcome_completed_when_all_succeed(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="t1",
        capability="stub_noop_concurrent",
        status=TaskStatus.SUCCEEDED,
    )

    async with db_session_factory() as db:
        status, failure_class = await determine_run_outcome(db, run_id)
        assert status == RunStatus.COMPLETED
        assert failure_class is None


async def test_run_stall_sweep_flags_long_running_task(db_session_factory, redis_client):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    stale_start = datetime.now(UTC) - timedelta(seconds=STALL_THRESHOLD_SECONDS + 60)
    task_id = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="t1",
        capability="stub_noop_concurrent",
        status=TaskStatus.RUNNING,
        started_at=stale_start,
    )

    async with db_session_factory() as db:
        flagged = await run_stall_sweep_once(db, redis_client)
    assert task_id in flagged

    async with db_session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.stalled_flagged_at is not None

    # Running the sweep again must not re-flag (and re-emit an event for)
    # the same task.
    async with db_session_factory() as db:
        flagged_again = await run_stall_sweep_once(db, redis_client)
    assert flagged_again == []


async def test_run_stall_sweep_ignores_recent_running_tasks(db_session_factory, redis_client):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="t1",
        capability="stub_noop_concurrent",
        status=TaskStatus.RUNNING,
        started_at=datetime.now(UTC),
    )

    async with db_session_factory() as db:
        flagged = await run_stall_sweep_once(db, redis_client)
    assert flagged == []
