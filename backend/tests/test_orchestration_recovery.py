from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency
from src.orchestration.recovery import reap_incomplete_runs
from src.orchestration.task_engine import claim_task, drive_run_to_quiescence, execute_claimed_task


async def test_reap_resets_claimed_and_running_tasks_and_lets_run_progress(
    db_session_factory, redis_client
):
    """Simulates a process kill: a run RUNNING with one task CLAIMED by a
    now-dead worker. reap_incomplete_runs() must reset that task to READY
    so a fresh worker can claim and complete it -- the run "recovers".
    """
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
        await db.flush()
        task = Task(
            run_id=run.id,
            plan_id=plan.id,
            plan_key="t1",
            capability="stub_noop_concurrent",
            name="t1",
            status=TaskStatus.CLAIMED,
            claimed_by="dead-worker",
        )
        db.add(task)
        await db.commit()
        run_id, task_id = run.id, task.id

    async with db_session_factory() as db:
        reaped = await reap_incomplete_runs(db, redis_client)
    assert run_id in reaped

    async with db_session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == TaskStatus.READY
        assert task.claimed_by is None

    # And a fresh worker can now genuinely claim and finish it.
    async with db_session_factory() as db:
        claimed = await claim_task(db, task_id, "new-worker")
        assert claimed is True

    async with db_session_factory() as db:
        task = await db.get(Task, task_id)
        await execute_claimed_task(db, redis_client, task)

    async with db_session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == TaskStatus.SUCCEEDED


async def test_reap_then_drive_completes_a_run_killed_mid_flight(db_session_factory, redis_client):
    """The full "kill the process mid-run and see it recover on restart"
    acceptance scenario: a run with one task already SUCCEEDED, one
    CLAIMED by a now-dead worker, and a third still PENDING on the dead
    one. reap_incomplete_runs() (crash recovery) followed by
    drive_run_to_quiescence() (what main.py's lifespan fires automatically
    for every reaped run) must carry the whole run through to COMPLETED --
    not just make one task reclaimable.
    """
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
        await db.flush()

        done = Task(
            run_id=run.id,
            plan_id=plan.id,
            plan_key="done",
            capability="stub_noop_concurrent",
            name="done",
            status=TaskStatus.SUCCEEDED,
        )
        stuck = Task(
            run_id=run.id,
            plan_id=plan.id,
            plan_key="stuck",
            capability="stub_noop_concurrent",
            name="stuck",
            status=TaskStatus.CLAIMED,
            claimed_by="dead-worker",
        )
        db.add_all([done, stuck])
        await db.flush()

        waiting = Task(
            run_id=run.id,
            plan_id=plan.id,
            plan_key="waiting",
            capability="stub_noop_concurrent",
            name="waiting",
            status=TaskStatus.PENDING,
        )
        db.add(waiting)
        await db.flush()
        db.add(TaskDependency(task_id=waiting.id, depends_on_task_id=stuck.id))
        await db.commit()
        run_id = run.id

    async with db_session_factory() as db:
        reaped = await reap_incomplete_runs(db, redis_client)
    assert run_id in reaped

    final_status = await drive_run_to_quiescence(db_session_factory, redis_client, run_id)
    assert final_status == RunStatus.COMPLETED

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.COMPLETED


async def test_reap_leaves_paused_runs_untouched(db_session_factory, redis_client):
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj", source=RunSource.UI, run_type=RunType.STANDARD, status=RunStatus.PAUSED
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async with db_session_factory() as db:
        reaped = await reap_incomplete_runs(db, redis_client)
    assert run_id not in reaped

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.PAUSED


async def test_reap_leaves_terminal_runs_untouched(db_session_factory, redis_client):
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.COMPLETED,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async with db_session_factory() as db:
        reaped = await reap_incomplete_runs(db, redis_client)
    assert run_id not in reaped
