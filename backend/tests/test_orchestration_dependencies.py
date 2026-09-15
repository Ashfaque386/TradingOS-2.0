import uuid

from sqlalchemy import select

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.result_artefact import ResultArtefact
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency
from src.orchestration.dependencies import propagate_unsatisfiable, resolve_dependencies_on_success


async def _make_run_and_plan(db_session_factory) -> tuple[uuid.UUID, uuid.UUID]:
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
    db_session_factory, run_id, plan_id, *, plan_key: str, status: TaskStatus = TaskStatus.PENDING
) -> uuid.UUID:
    async with db_session_factory() as db:
        task = Task(
            run_id=run_id,
            plan_id=plan_id,
            plan_key=plan_key,
            capability="stub_noop_concurrent",
            name=plan_key,
            status=status,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task.id


async def test_dependent_task_ready_only_once_all_dependencies_satisfied(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    upstream_a = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="a",
        status=TaskStatus.RUNNING,
    )
    upstream_b = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="b",
        status=TaskStatus.RUNNING,
    )
    dependent = await _add_task(db_session_factory, run_id, plan_id, plan_key="c")

    async with db_session_factory() as db:
        db.add(TaskDependency(task_id=dependent, depends_on_task_id=upstream_a))
        db.add(TaskDependency(task_id=dependent, depends_on_task_id=upstream_b))
        await db.commit()

    async with db_session_factory() as db:
        await resolve_dependencies_on_success(db, upstream_a)

    async with db_session_factory() as db:
        task = await db.get(Task, dependent)
        assert task.status == TaskStatus.PENDING  # only one of two deps satisfied

    async with db_session_factory() as db:
        await resolve_dependencies_on_success(db, upstream_b)

    async with db_session_factory() as db:
        task = await db.get(Task, dependent)
        assert task.status == TaskStatus.READY


async def test_typed_dependency_requires_matching_artefact_type(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    upstream = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="a",
        status=TaskStatus.RUNNING,
    )
    dependent = await _add_task(db_session_factory, run_id, plan_id, plan_key="b")

    async with db_session_factory() as db:
        db.add(
            TaskDependency(
                task_id=dependent, depends_on_task_id=upstream, artefact_type="expected_type"
            )
        )
        await db.commit()

    # Upstream succeeds but produces a DIFFERENT artefact type -- the typed
    # dependency must not be satisfied by success alone.
    async with db_session_factory() as db:
        db.add(
            ResultArtefact(run_id=run_id, task_id=upstream, artefact_type="other_type", payload={})
        )
        await db.commit()
        await resolve_dependencies_on_success(db, upstream)

    async with db_session_factory() as db:
        task = await db.get(Task, dependent)
        assert task.status == TaskStatus.PENDING

        dep = (
            await db.execute(select(TaskDependency).where(TaskDependency.task_id == dependent))
        ).scalar_one()
        assert dep.satisfied is False


async def test_permanent_failure_cascades_unsatisfiable_transitively(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    root = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="root",
        status=TaskStatus.FAILED,
    )
    child = await _add_task(db_session_factory, run_id, plan_id, plan_key="child")
    grandchild = await _add_task(db_session_factory, run_id, plan_id, plan_key="grandchild")

    async with db_session_factory() as db:
        db.add(TaskDependency(task_id=child, depends_on_task_id=root))
        db.add(TaskDependency(task_id=grandchild, depends_on_task_id=child))
        await db.commit()

    async with db_session_factory() as db:
        await propagate_unsatisfiable(db, root)

    async with db_session_factory() as db:
        assert (await db.get(Task, child)).status == TaskStatus.UNSATISFIABLE
        assert (await db.get(Task, grandchild)).status == TaskStatus.UNSATISFIABLE


async def test_unsatisfiable_does_not_touch_already_terminal_tasks(db_session_factory):
    run_id, plan_id = await _make_run_and_plan(db_session_factory)
    root = await _add_task(
        db_session_factory,
        run_id,
        plan_id,
        plan_key="root",
        status=TaskStatus.FAILED,
    )
    already_succeeded = await _add_task(
        db_session_factory, run_id, plan_id, plan_key="done", status=TaskStatus.SUCCEEDED
    )

    async with db_session_factory() as db:
        db.add(TaskDependency(task_id=already_succeeded, depends_on_task_id=root))
        await db.commit()

    async with db_session_factory() as db:
        await propagate_unsatisfiable(db, root)

    async with db_session_factory() as db:
        assert (await db.get(Task, already_succeeded)).status == TaskStatus.SUCCEEDED
