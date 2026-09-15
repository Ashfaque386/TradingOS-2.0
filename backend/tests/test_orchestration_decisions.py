import uuid

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.result_artefact import ResultArtefact
from src.orchestration.decisions import detect_conflicts, record_decision


async def _make_run(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def _make_artefact(db_session_factory, run_id: uuid.UUID) -> uuid.UUID:
    # A ResultArtefact needs a task_id FK; this phase's conflict detector
    # only cares about the artefact's id/run_id, so a throwaway task row is
    # enough to satisfy the FK without pulling in the full task-engine flow.
    from src.models.organizational_plan import OrganizationalPlan, PlanStatus
    from src.models.task import Task

    async with db_session_factory() as db:
        plan = OrganizationalPlan(
            run_id=run_id, attempt_number=1, status=PlanStatus.ACCEPTED, objective="obj"
        )
        db.add(plan)
        await db.flush()
        task = Task(
            run_id=run_id,
            plan_id=plan.id,
            plan_key="t",
            capability="stub_noop_concurrent",
            name="t",
        )
        db.add(task)
        await db.flush()
        artefact = ResultArtefact(
            run_id=run_id, task_id=task.id, artefact_type="strategy", payload={}
        )
        db.add(artefact)
        await db.commit()
        await db.refresh(artefact)
        return artefact.id


async def test_no_conflict_with_a_single_decision(db_session_factory):
    run_id = await _make_run(db_session_factory)
    artefact_id = await _make_artefact(db_session_factory, run_id)

    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="review",
            verdict="approve",
            subject_artefact_id=artefact_id,
        )

    async with db_session_factory() as db:
        conflicts = await detect_conflicts(db, run_id)
    assert conflicts == []


async def test_contradictory_verdicts_on_same_artefact_flagged(db_session_factory):
    run_id = await _make_run(db_session_factory)
    artefact_id = await _make_artefact(db_session_factory, run_id)

    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="review",
            verdict="approve",
            subject_artefact_id=artefact_id,
        )
    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="review",
            verdict="reject",
            subject_artefact_id=artefact_id,
        )

    async with db_session_factory() as db:
        conflicts = await detect_conflicts(db, run_id)

    assert len(conflicts) == 1
    assert conflicts[0].subject_artefact_id == artefact_id
    assert conflicts[0].decision_type == "review"
    assert set(conflicts[0].verdicts) == {"approve", "reject"}
    assert len(conflicts[0].conflicting_decision_ids) == 2


async def test_agreeing_verdicts_on_same_artefact_not_flagged(db_session_factory):
    run_id = await _make_run(db_session_factory)
    artefact_id = await _make_artefact(db_session_factory, run_id)

    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="review",
            verdict="approve",
            subject_artefact_id=artefact_id,
        )
    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="review",
            verdict="approve",
            subject_artefact_id=artefact_id,
        )

    async with db_session_factory() as db:
        conflicts = await detect_conflicts(db, run_id)
    assert conflicts == []


async def test_different_decision_types_on_same_artefact_not_grouped_together(db_session_factory):
    run_id = await _make_run(db_session_factory)
    artefact_id = await _make_artefact(db_session_factory, run_id)

    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="market_signal",
            verdict="bullish",
            subject_artefact_id=artefact_id,
        )
    async with db_session_factory() as db:
        await record_decision(
            db,
            run_id=run_id,
            decision_type="sentiment_signal",
            verdict="bearish",
            subject_artefact_id=artefact_id,
        )

    async with db_session_factory() as db:
        conflicts = await detect_conflicts(db, run_id)
    # Different decision_type buckets -- structurally not the same
    # comparison, so no conflict at this generic layer (a concrete
    # market-vs-sentiment conflict type is a Phase 3+ agent's concern).
    assert conflicts == []


async def test_decisions_without_a_subject_artefact_are_ignored(db_session_factory):
    run_id = await _make_run(db_session_factory)

    async with db_session_factory() as db:
        await record_decision(db, run_id=run_id, decision_type="freeform", verdict="ok")
    async with db_session_factory() as db:
        await record_decision(db, run_id=run_id, decision_type="freeform", verdict="not_ok")

    async with db_session_factory() as db:
        conflicts = await detect_conflicts(db, run_id)
    assert conflicts == []
