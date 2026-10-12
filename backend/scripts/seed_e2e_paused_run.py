"""E2E test-fixture seeding: a PAUSED organization run with a mixed task graph.

Why a script: every capability in the organization-run engine is an instant
stub, so a run created through the API is already COMPLETED by the time the
request returns -- there is no way to reach a paused (or any other
cancellable) run through the UI/API alone. e2e/run-cancel.spec.ts needs one to
exercise the real cancel flow end to end, so this plants it directly in the
database, the same way seed_e2e_live_intent.py plants a live-eligible
strategy. Everything the spec asserts afterwards -- the confirm step, the
request, the resulting statuses -- goes through the real UI and API.

Each run gets its own uniquely-named objective (the shared dev DB keeps every
previous run around, and the spec locates its card by exact objective).

Usage:
    python scripts/seed_e2e_paused_run.py
"""

import asyncio
import uuid
from datetime import UTC, datetime

from src.core.db import get_session_factory
from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus

# (plan_key, capability, status): one finished task that must keep its
# outcome, one waiting to run, one blocked behind it.
_TASKS = (
    ("research", "research_scaffold_news", TaskStatus.SUCCEEDED),
    ("analysis", "research_scaffold_market_analysis", TaskStatus.READY),
    ("backtest", "backtesting", TaskStatus.PENDING),
)


async def seed() -> None:
    session_factory = get_session_factory()
    objective = f"E2E paused run {uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with session_factory() as db:
        run = OrganizationRun(
            objective=objective,
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.PAUSED,
            started_at=now,
        )
        db.add(run)
        await db.flush()
        plan = OrganizationalPlan(
            run_id=run.id, attempt_number=1, status=PlanStatus.ACCEPTED, objective=objective
        )
        db.add(plan)
        await db.flush()
        for key, capability, status in _TASKS:
            db.add(
                Task(
                    run_id=run.id,
                    plan_id=plan.id,
                    plan_key=key,
                    capability=capability,
                    name=key,
                    status=status,
                    started_at=now if status == TaskStatus.SUCCEEDED else None,
                    completed_at=now if status == TaskStatus.SUCCEEDED else None,
                )
            )
        await db.commit()
        run_id = run.id

    print(f"run_id={run_id}")
    print(f"objective={objective}")


if __name__ == "__main__":
    asyncio.run(seed())
