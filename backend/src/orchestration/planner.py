"""Planner (Build Spec §7.3): given an objective, produces a task graph and
deterministically validates it before any task is materialized.

fake_llm_planner() stands in for the real LLM router (Phase 3) — same
objective always produces the same graph, no network calls, no randomness.
The *validator* (validate_plan_graph and friends) is what's actually under
test here and is exercised directly with hand-built graphs, not only
indirectly through the fake planner's own (always-valid) output.
"""

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organization_run import OrganizationRun, RunStatus
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency
from src.orchestration.capabilities import KNOWN_CAPABILITIES

logger = structlog.get_logger(__name__)

# Build Spec §7.3: fixed safety order enforced whenever these capabilities
# appear in a plan together — a later one in this tuple must transitively
# depend on every earlier one that's also present.
SAFETY_ORDER: tuple[str, ...] = (
    "code_validation",
    "compliance_check",
    "backtesting",
    "strategy_evaluation",
    "risk_assessment",
)

MAX_PLAN_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DependencySpec:
    depends_on_key: str
    # None = a plain ordering dependency, satisfied by the upstream task's
    # success alone. Set = additionally requires a matching ResultArtefact
    # (src/orchestration/dependencies.py) — a "typed artefact dependency".
    artefact_type: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSpec:
    key: str  # plan-local identifier, stable across the plan's own graph
    capability: str
    name: str
    params: dict = field(default_factory=dict)
    depends_on: tuple[DependencySpec, ...] = ()
    produces_artefact_type: str | None = None
    timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class PlanGraph:
    tasks: tuple[TaskSpec, ...]


PlannerFn = Callable[[str, int], PlanGraph]


class CannotPlanError(Exception):
    def __init__(self, run_id: uuid.UUID, attempts_errors: list[list[str]]):
        self.run_id = run_id
        self.attempts_errors = attempts_errors
        super().__init__(
            f"planning failed for run {run_id} after {len(attempts_errors)} attempt(s)"
        )


def fake_llm_planner(objective: str, attempt: int) -> PlanGraph:
    """Deterministic stand-in for the real LLM planner (Phase 3): always
    prepends the research scaffold (Build Spec §7.3 "research scaffold
    injector"), then a fixed safety-ordered pipeline. Same objective
    always produces the same graph.
    """
    scaffold = (
        TaskSpec(
            key="news",
            capability="research_scaffold_news",
            name="News scan",
            params={"objective": objective},
        ),
        TaskSpec(
            key="sentiment",
            capability="research_scaffold_sentiment",
            name="Sentiment scan",
            depends_on=(DependencySpec("news"),),
        ),
        TaskSpec(
            key="market_analysis",
            capability="research_scaffold_market_analysis",
            name="Market analysis",
            depends_on=(DependencySpec("sentiment"),),
        ),
        TaskSpec(
            key="portfolio_read",
            capability="research_scaffold_portfolio_read",
            name="Portfolio read",
            depends_on=(DependencySpec("market_analysis"),),
        ),
    )
    pipeline = (
        TaskSpec(
            key="code_validation",
            capability="code_validation",
            name="Validate strategy code",
            depends_on=(DependencySpec("portfolio_read"),),
            produces_artefact_type="code_validation_result",
        ),
        TaskSpec(
            key="compliance_check",
            capability="compliance_check",
            name="Compliance check",
            depends_on=(DependencySpec("code_validation", artefact_type="code_validation_result"),),
        ),
        TaskSpec(
            key="backtesting",
            capability="backtesting",
            name="Backtest",
            depends_on=(DependencySpec("compliance_check"),),
            produces_artefact_type="backtest_result",
        ),
        TaskSpec(
            key="strategy_evaluation",
            capability="strategy_evaluation",
            name="Evaluate",
            depends_on=(DependencySpec("backtesting", artefact_type="backtest_result"),),
        ),
        TaskSpec(
            key="risk_assessment",
            capability="risk_assessment",
            name="Risk assessment",
            depends_on=(DependencySpec("strategy_evaluation"),),
        ),
    )
    return PlanGraph(tasks=scaffold + pipeline)


def _direct_deps(graph: PlanGraph) -> dict[str, list[str]]:
    return {t.key: [d.depends_on_key for d in t.depends_on] for t in graph.tasks}


def _has_cycle(graph: PlanGraph) -> bool:
    direct = _direct_deps(graph)
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(direct, white)

    def visit(node: str) -> bool:
        color[node] = gray
        for nxt in direct.get(node, ()):
            state = color.get(nxt, white)
            if state == gray:
                return True
            if state == white and visit(nxt):
                return True
        color[node] = black
        return False

    return any(color[node] == white and visit(node) for node in direct)


def _ancestors(graph: PlanGraph) -> dict[str, set[str]]:
    direct = _direct_deps(graph)
    result: dict[str, set[str]] = {}
    for key in direct:
        seen: set[str] = set()
        stack = list(direct.get(key, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(direct.get(node, ()))
        result[key] = seen
    return result


def _check_safety_order(graph: PlanGraph) -> list[str]:
    by_capability: dict[str, list[TaskSpec]] = {}
    for t in graph.tasks:
        if t.capability in SAFETY_ORDER:
            by_capability.setdefault(t.capability, []).append(t)

    present_order = [cap for cap in SAFETY_ORDER if cap in by_capability]
    if len(present_order) < 2:
        return []

    ancestors = _ancestors(graph)
    errors: list[str] = []
    for later_idx in range(1, len(present_order)):
        later_cap = present_order[later_idx]
        for later_task in by_capability[later_cap]:
            later_ancestors = ancestors.get(later_task.key, set())
            for earlier_cap in present_order[:later_idx]:
                for earlier_task in by_capability[earlier_cap]:
                    if earlier_task.key not in later_ancestors:
                        errors.append(
                            f"safety order violation: task {later_task.key!r} "
                            f"({later_cap}) must transitively depend on task "
                            f"{earlier_task.key!r} ({earlier_cap})"
                        )
    return errors


def validate_plan_graph(graph: PlanGraph) -> list[str]:
    errors: list[str] = []
    keys = [t.key for t in graph.tasks]
    key_set = set(keys)
    if len(key_set) != len(keys):
        errors.append("duplicate task keys in plan")

    for t in graph.tasks:
        if t.capability not in KNOWN_CAPABILITIES:
            errors.append(f"unknown capability {t.capability!r} on task {t.key!r}")
        for dep in t.depends_on:
            if dep.depends_on_key not in key_set:
                errors.append(f"task {t.key!r} depends on unknown task {dep.depends_on_key!r}")

    # Cycle detection assumes every depends_on key resolves to a real task;
    # skip it if the graph already failed that check, rather than crashing
    # on a dangling edge.
    if not errors and _has_cycle(graph):
        errors.append("plan graph contains a cycle")

    errors.extend(_check_safety_order(graph))
    return errors


def _graph_to_json(graph: PlanGraph) -> dict:
    return {"tasks": [asdict(t) for t in graph.tasks]}


async def _materialize_tasks(
    db: AsyncSession, run_id: uuid.UUID, plan_id: uuid.UUID, graph: PlanGraph
) -> dict[str, uuid.UUID]:
    key_to_id: dict[str, uuid.UUID] = {}
    for t in graph.tasks:
        row = Task(
            run_id=run_id,
            plan_id=plan_id,
            plan_key=t.key,
            capability=t.capability,
            name=t.name,
            params=t.params,
            status=TaskStatus.PENDING if t.depends_on else TaskStatus.READY,
            timeout_seconds=t.timeout_seconds,
            produces_artefact_type=t.produces_artefact_type,
        )
        db.add(row)
        await db.flush()
        key_to_id[t.key] = row.id

    for t in graph.tasks:
        for dep in t.depends_on:
            db.add(
                TaskDependency(
                    task_id=key_to_id[t.key],
                    depends_on_task_id=key_to_id[dep.depends_on_key],
                    artefact_type=dep.artefact_type,
                )
            )
    return key_to_id


async def create_plan(
    db: AsyncSession,
    run_id: uuid.UUID,
    objective: str,
    *,
    planner_fn: PlannerFn = fake_llm_planner,
    max_attempts: int = MAX_PLAN_ATTEMPTS,
) -> OrganizationalPlan:
    """Runs up to `max_attempts` planner attempts; the first structurally
    valid graph is accepted and materialized into `tasks`/`task_dependencies`,
    flipping the run to RUNNING. Exhausting every attempt flips the run to
    CANNOT_PLAN and raises CannotPlanError.
    """
    attempts_errors: list[list[str]] = []

    for attempt in range(1, max_attempts + 1):
        graph = planner_fn(objective, attempt)
        errors = validate_plan_graph(graph)

        plan = OrganizationalPlan(
            run_id=run_id,
            attempt_number=attempt,
            status=PlanStatus.ACCEPTED if not errors else PlanStatus.REJECTED,
            objective=objective,
            planner_output=_graph_to_json(graph),
            validation_errors=errors or None,
        )
        db.add(plan)
        await db.flush()

        if not errors:
            await _materialize_tasks(db, run_id, plan.id, graph)
            await db.execute(
                update(OrganizationRun)
                .where(OrganizationRun.id == run_id)
                .values(status=RunStatus.RUNNING)
            )
            await db.commit()
            await db.refresh(plan)
            return plan

        logger.warning(
            "orchestration.plan_rejected", run_id=str(run_id), attempt=attempt, errors=errors
        )
        attempts_errors.append(errors)
        await db.commit()

    await db.execute(
        update(OrganizationRun)
        .where(OrganizationRun.id == run_id)
        .values(status=RunStatus.CANNOT_PLAN)
    )
    await db.commit()
    raise CannotPlanError(run_id, attempts_errors)
