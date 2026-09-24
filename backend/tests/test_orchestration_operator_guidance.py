"""Operator Guidance tests (Phase 19, docs/phase19-audit.md Part 2.6/3.1):
confirms guidance is a real input to the next planning cycle, not a note
nobody reads.
"""

from src.models.organization_run import RunSource
from src.orchestration.operator_guidance import (
    create_guidance,
    deactivate_guidance,
    fold_guidance_into_objective,
    list_guidance,
)
from src.orchestration.run_control import create_run


async def test_fold_guidance_appends_active_notes_to_objective(db_session_factory):
    async with db_session_factory() as db:
        await create_guidance(
            db, message="Prioritize low-volatility strategies", created_by="ops@example.com"
        )
        folded = await fold_guidance_into_objective(db, "Research new momentum strategies")

    assert "Research new momentum strategies" in folded
    assert "Prioritize low-volatility strategies" in folded


async def test_fold_guidance_is_a_noop_with_no_active_guidance(db_session_factory):
    async with db_session_factory() as db:
        folded = await fold_guidance_into_objective(db, "Research new momentum strategies")

    assert folded == "Research new momentum strategies"


async def test_deactivated_guidance_is_never_folded_in(db_session_factory):
    async with db_session_factory() as db:
        guidance = await create_guidance(db, message="Retired note", created_by="ops@example.com")
        await deactivate_guidance(db, guidance.id, actor="ops@example.com")
        folded = await fold_guidance_into_objective(db, "Research new momentum strategies")

    assert folded == "Research new momentum strategies"


async def test_create_run_folds_guidance_into_the_real_plan_objective(
    db_session_factory, redis_client
):
    async with db_session_factory() as db:
        await create_guidance(
            db, message="Favor equity, avoid options this week", created_by="ops@example.com"
        )

    run = await create_run(
        db_session_factory, redis_client, objective="Generate a new strategy", source=RunSource.UI
    )

    async with db_session_factory() as db:
        from sqlalchemy import select

        from src.models.organizational_plan import OrganizationalPlan

        plan = (
            (
                await db.execute(
                    select(OrganizationalPlan).where(OrganizationalPlan.run_id == run.id)
                )
            )
            .scalars()
            .first()
        )

    # The run's own objective stays exactly what the operator typed...
    assert run.objective == "Generate a new strategy"
    # ...but the real plan the planner acted on includes the guidance.
    assert "Favor equity, avoid options this week" in plan.objective


async def test_list_guidance_shows_both_active_and_inactive(db_session_factory):
    async with db_session_factory() as db:
        active = await create_guidance(db, message="Active note", created_by="a@example.com")
        inactive = await create_guidance(db, message="Inactive note", created_by="a@example.com")
        await deactivate_guidance(db, inactive.id, actor="a@example.com")

        all_rows = await list_guidance(db)
        active_only = await list_guidance(db, active_only=True)

    assert {r.id for r in all_rows} == {active.id, inactive.id}
    assert {r.id for r in active_only} == {active.id}
