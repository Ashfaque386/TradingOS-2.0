import pytest
from sqlalchemy import select

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.task import Task, TaskStatus
from src.orchestration.planner import (
    CannotPlanError,
    DependencySpec,
    PlanGraph,
    TaskSpec,
    create_plan,
    fake_llm_planner,
    validate_plan_graph,
)


def test_fake_planner_output_is_valid():
    graph = fake_llm_planner("Research a momentum strategy", attempt=1)
    assert validate_plan_graph(graph) == []


def test_fake_planner_is_deterministic():
    a = fake_llm_planner("same objective", attempt=1)
    b = fake_llm_planner("same objective", attempt=1)
    assert a == b


def test_validate_plan_graph_rejects_unknown_capability():
    graph = PlanGraph(tasks=(TaskSpec(key="a", capability="not_a_real_capability", name="A"),))
    errors = validate_plan_graph(graph)
    assert any("unknown capability" in e for e in errors)


def test_validate_plan_graph_rejects_cycle():
    graph = PlanGraph(
        tasks=(
            TaskSpec(
                key="a",
                capability="stub_noop_concurrent",
                name="A",
                depends_on=(DependencySpec("b"),),
            ),
            TaskSpec(
                key="b",
                capability="stub_noop_concurrent",
                name="B",
                depends_on=(DependencySpec("a"),),
            ),
        )
    )
    errors = validate_plan_graph(graph)
    assert any("cycle" in e for e in errors)


def test_validate_plan_graph_rejects_unknown_dependency():
    graph = PlanGraph(
        tasks=(
            TaskSpec(
                key="a",
                capability="stub_noop_concurrent",
                name="A",
                depends_on=(DependencySpec("ghost"),),
            ),
        )
    )
    errors = validate_plan_graph(graph)
    assert any("unknown task" in e for e in errors)


def test_validate_plan_graph_rejects_duplicate_keys():
    graph = PlanGraph(
        tasks=(
            TaskSpec(key="a", capability="stub_noop_concurrent", name="A"),
            TaskSpec(key="a", capability="stub_noop_serial", name="A2"),
        )
    )
    errors = validate_plan_graph(graph)
    assert any("duplicate" in e for e in errors)


def test_validate_plan_graph_rejects_safety_order_violation():
    # backtesting present without depending (even transitively) on
    # code_validation, which is also present -- violates the fixed order.
    graph = PlanGraph(
        tasks=(
            TaskSpec(key="code_validation", capability="code_validation", name="CV"),
            TaskSpec(key="backtesting", capability="backtesting", name="BT"),
        )
    )
    errors = validate_plan_graph(graph)
    assert any("safety order" in e for e in errors)


def test_validate_plan_graph_accepts_correct_safety_order():
    graph = PlanGraph(
        tasks=(
            TaskSpec(key="code_validation", capability="code_validation", name="CV"),
            TaskSpec(
                key="backtesting",
                capability="backtesting",
                name="BT",
                depends_on=(DependencySpec("code_validation"),),
            ),
        )
    )
    assert validate_plan_graph(graph) == []


async def _make_pending_run(db_session_factory, objective: str = "obj"):
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective=objective,
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.PLANNING,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def test_create_plan_materializes_tasks_and_starts_run(db_session_factory):
    run_id = await _make_pending_run(db_session_factory)

    async with db_session_factory() as db:
        plan = await create_plan(db, run_id, "obj")
        assert plan.status.value == "accepted"

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.RUNNING

        result = await db.execute(select(Task).where(Task.run_id == run_id))
        tasks = list(result.scalars())
        assert len(tasks) == 9  # 4 research-scaffold + 5 safety-ordered pipeline

        news_task = next(t for t in tasks if t.plan_key == "news")
        assert news_task.status == TaskStatus.READY

        sentiment_task = next(t for t in tasks if t.plan_key == "sentiment")
        assert sentiment_task.status == TaskStatus.PENDING


async def test_create_plan_gives_up_after_max_attempts(db_session_factory):
    def always_bad_planner(objective: str, attempt: int) -> PlanGraph:
        return PlanGraph(tasks=(TaskSpec(key="a", capability="nonexistent", name="A"),))

    run_id = await _make_pending_run(db_session_factory)

    async with db_session_factory() as db:
        with pytest.raises(CannotPlanError) as exc_info:
            await create_plan(db, run_id, "obj", planner_fn=always_bad_planner)
        assert len(exc_info.value.attempts_errors) == 3

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.CANNOT_PLAN
