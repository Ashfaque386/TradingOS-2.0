"""Cancelling an organization run (backlog item 21).

What a cancel must guarantee, and what these pin:
- it is atomic and race-safe against pause / continue / a second cancel;
- every unfinished task ends CANCELLED, finished ones keep their outcome;
- a capability already executing in its (uninterruptible) thread cannot undo
  the cancel when it eventually returns -- its result is discarded, no artefact
  is written, no dependent is woken, no failure cascades;
- a cancel landing mid-planning is not overwritten back to RUNNING;
- crash recovery, the stall sweep, retry and continue all leave a cancelled
  run alone, while rerun still works on it;
- it is audited when it happens and not when there was nothing to cancel.
"""

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.organization_run import (
    CANCELLABLE_STATUSES,
    TERMINAL_STATUSES,
    OrganizationRun,
    RunSource,
    RunStatus,
    RunType,
)
from src.models.organizational_event import OrganizationalEvent
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.result_artefact import ResultArtefact
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency
from src.orchestration import run_control
from src.orchestration.capabilities import REGISTRY, PermanentCapabilityError
from src.orchestration.planner import PlanGraph, TaskSpec
from src.orchestration.recovery import reap_incomplete_runs
from src.orchestration.run_control import CancelResult, cancel_run
from src.orchestration.task_engine import (
    claim_task,
    drive_run_to_quiescence,
    execute_claimed_task,
    run_stall_sweep_once,
)

ACTOR = "operator@example.com"

# (plan_key, capability, status)
TaskSeed = tuple[str, str, TaskStatus]


async def _seed_run(
    db_session_factory,
    *,
    status: RunStatus = RunStatus.RUNNING,
    tasks: list[TaskSeed] | None = None,
    depends: list[tuple[str, str]] | None = None,
    produces: dict[str, str] | None = None,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """A run with hand-placed tasks. `depends` is [(task_key, depends_on_key)];
    `produces` maps a task key to the artefact type it declares."""
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj", source=RunSource.UI, run_type=RunType.STANDARD, status=status
        )
        db.add(run)
        await db.flush()
        plan = OrganizationalPlan(
            run_id=run.id, attempt_number=1, status=PlanStatus.ACCEPTED, objective="obj"
        )
        db.add(plan)
        await db.flush()
        ids: dict[str, uuid.UUID] = {}
        for key, capability, task_status in tasks or []:
            task = Task(
                run_id=run.id,
                plan_id=plan.id,
                plan_key=key,
                capability=capability,
                name=key,
                status=task_status,
                produces_artefact_type=(produces or {}).get(key),
                started_at=(
                    datetime.now(UTC)
                    if task_status in (TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
                    else None
                ),
            )
            db.add(task)
            await db.flush()
            ids[key] = task.id
        for key, depends_on in depends or []:
            db.add(TaskDependency(task_id=ids[key], depends_on_task_id=ids[depends_on]))
        await db.commit()
        return run.id, ids


async def _run_status(db_session_factory, run_id: uuid.UUID) -> RunStatus:
    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run is not None
        return run.status


async def _task_statuses(db_session_factory, run_id: uuid.UUID) -> dict[str, TaskStatus]:
    async with db_session_factory() as db:
        rows = (await db.execute(select(Task).where(Task.run_id == run_id))).scalars().all()
        return {t.plan_key: t.status for t in rows}


async def _event_types(db_session_factory, run_id: uuid.UUID) -> list[str]:
    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(OrganizationalEvent)
                    .where(OrganizationalEvent.run_id == run_id)
                    .order_by(OrganizationalEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        return [e.event_type for e in rows]


async def _cancel(db_session_factory, redis_client, run_id: uuid.UUID) -> CancelResult | None:
    async with db_session_factory() as db:
        return await cancel_run(db, redis_client, run_id, actor=ACTOR)


# ---- the model -----------------------------------------------------------------


def test_cancelled_is_terminal_and_cancellable_is_everything_not_yet_terminal():
    assert RunStatus.CANCELLED in TERMINAL_STATUSES
    assert RunStatus.CANCELLED not in CANCELLABLE_STATUSES
    assert CANCELLABLE_STATUSES == {
        RunStatus.PENDING,
        RunStatus.PLANNING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
    }
    assert CANCELLABLE_STATUSES.isdisjoint(TERMINAL_STATUSES)


# ---- the transition ------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(CANCELLABLE_STATUSES, key=lambda s: s.value))
async def test_cancel_stops_a_run_from_every_non_terminal_state(
    db_session_factory, redis_client, status
):
    run_id, _ = await _seed_run(
        db_session_factory,
        status=status,
        tasks=[
            ("pending", "stub_noop_serial", TaskStatus.PENDING),
            ("ready", "stub_noop_serial", TaskStatus.READY),
            ("claimed", "stub_noop_serial", TaskStatus.CLAIMED),
            ("running", "stub_noop_serial", TaskStatus.RUNNING),
            ("done", "stub_noop_serial", TaskStatus.SUCCEEDED),
            ("broke", "stub_noop_serial", TaskStatus.FAILED),
            ("orphan", "stub_noop_serial", TaskStatus.UNSATISFIABLE),
        ],
    )

    result = await _cancel(db_session_factory, redis_client, run_id)

    assert result == CancelResult(tasks_cancelled=4, tasks_in_flight=2)
    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run is not None
        assert run.status == RunStatus.CANCELLED
        assert run.completed_at is not None
        assert run.failure_class is None and run.error is None  # not a failure
    assert await _task_statuses(db_session_factory, run_id) == {
        "pending": TaskStatus.CANCELLED,
        "ready": TaskStatus.CANCELLED,
        "claimed": TaskStatus.CANCELLED,
        "running": TaskStatus.CANCELLED,
        "done": TaskStatus.SUCCEEDED,  # finished work keeps its outcome
        "broke": TaskStatus.FAILED,
        "orphan": TaskStatus.UNSATISFIABLE,
    }


@pytest.mark.parametrize(
    "status",
    [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANNOT_PLAN, RunStatus.CANCELLED],
)
async def test_a_finished_run_cannot_be_cancelled_and_nothing_changes(
    db_session_factory, redis_client, status
):
    run_id, _ = await _seed_run(
        db_session_factory,
        status=status,
        tasks=[("t", "stub_noop_serial", TaskStatus.SUCCEEDED)],
    )

    assert await _cancel(db_session_factory, redis_client, run_id) is None

    assert await _run_status(db_session_factory, run_id) == status
    assert await _task_statuses(db_session_factory, run_id) == {"t": TaskStatus.SUCCEEDED}
    assert "run.cancelled" not in await _event_types(db_session_factory, run_id)


async def test_cancelling_an_unknown_run_does_nothing(db_session_factory, redis_client):
    assert await _cancel(db_session_factory, redis_client, uuid.uuid4()) is None


async def test_a_cancel_emits_one_event_naming_who_and_how_much(db_session_factory, redis_client):
    run_id, _ = await _seed_run(
        db_session_factory,
        tasks=[
            ("a", "stub_noop_serial", TaskStatus.RUNNING),
            ("b", "stub_noop_serial", TaskStatus.PENDING),
        ],
    )

    await _cancel(db_session_factory, redis_client, run_id)

    async with db_session_factory() as db:
        events = (
            (
                await db.execute(
                    select(OrganizationalEvent).where(
                        OrganizationalEvent.run_id == run_id,
                        OrganizationalEvent.event_type == "run.cancelled",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].payload == {
        "cancelled_by": ACTOR,
        "tasks_cancelled": 2,
        "tasks_in_flight": 1,
    }


# ---- races ---------------------------------------------------------------------


async def test_two_concurrent_cancels_one_wins(db_session_factory, redis_client):
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.RUNNING)

    results = await asyncio.gather(
        _cancel(db_session_factory, redis_client, run_id),
        _cancel(db_session_factory, redis_client, run_id),
    )

    assert sorted(r is not None for r in results) == [False, True]
    assert await _run_status(db_session_factory, run_id) == RunStatus.CANCELLED


async def test_cancel_always_ends_cancelled_even_when_racing_a_pause(
    db_session_factory, redis_client
):
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.RUNNING)

    async def _pause() -> bool:
        async with db_session_factory() as db:
            return await run_control.pause_run(db, redis_client, run_id)

    cancelled, _paused = await asyncio.gather(
        _cancel(db_session_factory, redis_client, run_id), _pause()
    )

    # Pause may land first (RUNNING -> PAUSED) and cancel then applies from
    # PAUSED, or cancel lands first and pause is a no-op -- either way the
    # operator's cancel is never lost.
    assert cancelled is not None
    assert await _run_status(db_session_factory, run_id) == RunStatus.CANCELLED


async def test_cancel_racing_a_continue_leaves_one_consistent_outcome(
    db_session_factory, redis_client
):
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.PAUSED)

    cancelled, continued = await asyncio.gather(
        _cancel(db_session_factory, redis_client, run_id),
        run_control.continue_run(db_session_factory, redis_client, run_id),
    )

    final = await _run_status(db_session_factory, run_id)
    if cancelled is not None:
        # Cancel landed -- before continue (continue then found nothing paused)
        # or just after it flipped the run to RUNNING (cancel applies there too).
        # Either way the cancel is final and continue's drive stops at once.
        assert final == RunStatus.CANCELLED
    else:
        # Continue won outright and drained the empty run to COMPLETED before
        # the cancel arrived; a finished run is not cancellable.
        assert final == RunStatus.COMPLETED
        assert continued is True


# ---- an in-flight task cannot undo a cancel ------------------------------------


class _Gate:
    """A capability whose thread blocks until released, so a cancel can land
    while it is genuinely executing."""

    def __init__(self, outcome: str = "succeed") -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.outcome = outcome
        self.calls = 0

    def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=15), "gate never released"
        if self.outcome == "fail":
            raise PermanentCapabilityError("failed after the run was cancelled")
        return {"ok": True}


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True}


@pytest.fixture
def capabilities(monkeypatch: pytest.MonkeyPatch):
    def _install(**fns: Any) -> None:
        for name, fn in fns.items():
            monkeypatch.setitem(REGISTRY._fns, name, fn)

    return _install


async def _drive_and_cancel_midflight(
    db_session_factory, redis_client, gate: _Gate, ids: dict[str, uuid.UUID], run_id: uuid.UUID
) -> RunStatus:
    driver = asyncio.create_task(drive_run_to_quiescence(db_session_factory, redis_client, run_id))
    try:
        assert await asyncio.to_thread(gate.entered.wait, 15), "capability never started"
        # It has flipped to RUNNING before the capability ran.
        assert (await _task_statuses(db_session_factory, run_id))["a"] == TaskStatus.RUNNING

        result = await _cancel(db_session_factory, redis_client, run_id)
        assert result == CancelResult(tasks_cancelled=2, tasks_in_flight=1)
    finally:
        gate.release.set()  # never strand the executor thread if an assertion above fails
    return await asyncio.wait_for(driver, timeout=15)


async def test_a_task_that_finishes_after_the_cancel_is_discarded(
    db_session_factory, redis_client, capabilities
):
    gate, spy = _Gate(), _Spy()
    capabilities(test_gate=gate, test_spy=spy)
    run_id, ids = await _seed_run(
        db_session_factory,
        tasks=[("a", "test_gate", TaskStatus.READY), ("b", "test_spy", TaskStatus.PENDING)],
        depends=[("b", "a")],
        produces={"a": "report"},
    )

    final = await _drive_and_cancel_midflight(db_session_factory, redis_client, gate, ids, run_id)

    assert final == RunStatus.CANCELLED  # the driver noticed and stopped
    assert await _run_status(db_session_factory, run_id) == RunStatus.CANCELLED
    statuses = await _task_statuses(db_session_factory, run_id)
    assert statuses["a"] == TaskStatus.CANCELLED  # not resurrected to SUCCEEDED
    assert statuses["b"] == TaskStatus.CANCELLED  # not promoted to READY by a's late success
    assert spy.calls == 0  # b's capability never ran
    async with db_session_factory() as db:
        assert (await db.execute(select(ResultArtefact))).scalars().all() == []
    events = await _event_types(db_session_factory, run_id)
    assert "task.result_discarded" in events and "task.succeeded" not in events
    assert gate.calls == 1  # the work did happen -- it just was not kept


async def test_a_task_that_fails_after_the_cancel_does_not_fail_or_cascade(
    db_session_factory, redis_client, capabilities
):
    gate, spy = _Gate(outcome="fail"), _Spy()
    capabilities(test_gate=gate, test_spy=spy)
    run_id, ids = await _seed_run(
        db_session_factory,
        tasks=[("a", "test_gate", TaskStatus.READY), ("b", "test_spy", TaskStatus.PENDING)],
        depends=[("b", "a")],
    )

    final = await _drive_and_cancel_midflight(db_session_factory, redis_client, gate, ids, run_id)

    assert final == RunStatus.CANCELLED
    statuses = await _task_statuses(db_session_factory, run_id)
    assert statuses["a"] == TaskStatus.CANCELLED  # not marked FAILED
    assert statuses["b"] == TaskStatus.CANCELLED  # not cascaded to UNSATISFIABLE
    events = await _event_types(db_session_factory, run_id)
    assert "task.result_discarded" in events and "task.failed" not in events
    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run is not None and run.failure_class is None


async def test_a_task_cancelled_between_claim_and_start_never_runs(
    db_session_factory, redis_client, capabilities
):
    spy = _Spy()
    capabilities(test_spy=spy)
    run_id, ids = await _seed_run(db_session_factory, tasks=[("a", "test_spy", TaskStatus.READY)])
    async with db_session_factory() as db:
        assert await claim_task(db, ids["a"], "worker-1") is True
    async with db_session_factory() as claiming_session:
        claimed_task = await claiming_session.get(Task, ids["a"])
        assert claimed_task is not None and claimed_task.status == TaskStatus.CLAIMED

        await _cancel(db_session_factory, redis_client, run_id)  # lands before the start
        await execute_claimed_task(claiming_session, redis_client, claimed_task)

    assert spy.calls == 0
    assert (await _task_statuses(db_session_factory, run_id))["a"] == TaskStatus.CANCELLED


async def test_the_driver_dispatches_nothing_for_a_run_cancelled_before_it_starts(
    db_session_factory, redis_client, capabilities
):
    spy = _Spy()
    capabilities(test_spy=spy)
    run_id, _ = await _seed_run(db_session_factory, tasks=[("a", "test_spy", TaskStatus.READY)])
    await _cancel(db_session_factory, redis_client, run_id)

    assert (
        await drive_run_to_quiescence(db_session_factory, redis_client, run_id)
        == RunStatus.CANCELLED
    )
    assert spy.calls == 0


# ---- a cancel during planning ---------------------------------------------------


async def test_a_cancel_during_planning_is_not_overwritten_and_leaves_no_orphans(
    db_session_factory, redis_client, monkeypatch: pytest.MonkeyPatch
):
    real_create_plan = run_control.create_plan

    async def cancelling_create_plan(db, run_id, objective, **kwargs):
        # The operator cancels while the planner is working.
        await _cancel(db_session_factory, redis_client, run_id)
        return await real_create_plan(db, run_id, objective, **kwargs)

    monkeypatch.setattr(run_control, "create_plan", cancelling_create_plan)

    run = await run_control.create_run(
        db_session_factory, redis_client, objective="plan me", source=RunSource.UI
    )

    assert run.status == RunStatus.CANCELLED  # not flipped back to RUNNING and run
    async with db_session_factory() as db:
        assert (await db.execute(select(Task).where(Task.run_id == run.id))).scalars().all() == []
        accepted_plans = (
            (
                await db.execute(
                    select(OrganizationalPlan).where(
                        OrganizationalPlan.run_id == run.id,
                        OrganizationalPlan.status == PlanStatus.ACCEPTED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert accepted_plans == []  # the plan produced after the cancel was rolled back
    events = await _event_types(db_session_factory, run.id)
    assert "run.cancelled" in events and "run.cannot_plan" not in events


async def test_a_cancel_during_a_failing_plan_is_not_reported_as_cannot_plan(
    db_session_factory, redis_client, monkeypatch: pytest.MonkeyPatch
):
    def always_bad(objective: str, attempt: int) -> PlanGraph:
        return PlanGraph(tasks=(TaskSpec(key="a", capability="nonexistent", name="A"),))

    real_create_plan = run_control.create_plan

    async def cancelling_create_plan(db, run_id, objective, **kwargs):
        await _cancel(db_session_factory, redis_client, run_id)
        return await real_create_plan(db, run_id, objective, **kwargs)

    monkeypatch.setattr(run_control, "create_plan", cancelling_create_plan)

    run = await run_control.create_run(
        db_session_factory,
        redis_client,
        objective="plan me",
        source=RunSource.UI,
        planner_fn=always_bad,
    )

    assert run.status == RunStatus.CANCELLED
    assert "run.cannot_plan" not in await _event_types(db_session_factory, run.id)


async def test_create_plan_without_a_cancel_still_plans_and_still_gives_up(
    db_session_factory, redis_client
):
    """The planning guards must not change the ordinary paths."""
    ok = await run_control.create_run(
        db_session_factory, redis_client, objective="fine", source=RunSource.UI
    )
    assert ok.status == RunStatus.COMPLETED

    def always_bad(objective: str, attempt: int) -> PlanGraph:
        return PlanGraph(tasks=(TaskSpec(key="a", capability="nonexistent", name="A"),))

    bad = await run_control.create_run(
        db_session_factory,
        redis_client,
        objective="hopeless",
        source=RunSource.UI,
        planner_fn=always_bad,
    )
    assert bad.status == RunStatus.CANNOT_PLAN


# ---- what leaves a cancelled run alone -----------------------------------------


async def test_crash_recovery_does_not_revive_a_cancelled_run(db_session_factory, redis_client):
    run_id, _ = await _seed_run(
        db_session_factory,
        status=RunStatus.CANCELLED,
        tasks=[
            ("ready", "stub_noop_serial", TaskStatus.READY),
            ("running", "stub_noop_serial", TaskStatus.RUNNING),
        ],
    )

    async with db_session_factory() as db:
        reaped = await reap_incomplete_runs(db, redis_client)

    assert run_id not in reaped
    assert await _run_status(db_session_factory, run_id) == RunStatus.CANCELLED
    assert (await _task_statuses(db_session_factory, run_id))["running"] == TaskStatus.RUNNING


async def test_the_stall_sweep_does_not_flag_a_cancelled_tasks_old_start(
    db_session_factory, redis_client
):
    run_id, ids = await _seed_run(
        db_session_factory,
        status=RunStatus.CANCELLED,
        tasks=[("old", "stub_noop_serial", TaskStatus.CANCELLED)],
    )
    async with db_session_factory() as db:
        task = await db.get(Task, ids["old"])
        assert task is not None
        task.started_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()

    async with db_session_factory() as db:
        assert await run_stall_sweep_once(db, redis_client) == []


async def test_retry_and_continue_refuse_a_cancelled_run_but_rerun_starts_afresh(
    db_session_factory, redis_client
):
    run_id, _ = await _seed_run(
        db_session_factory,
        status=RunStatus.CANCELLED,
        tasks=[("t", "stub_noop_serial", TaskStatus.CANCELLED)],
    )

    assert await run_control.retry_run(db_session_factory, redis_client, run_id) is False
    assert await run_control.continue_run(db_session_factory, redis_client, run_id) is False
    assert await _run_status(db_session_factory, run_id) == RunStatus.CANCELLED

    fresh = await run_control.rerun_run(db_session_factory, redis_client, run_id)
    assert fresh is not None
    assert fresh.run_type == RunType.RERUN and fresh.source_run_id == run_id
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.failure_class is None


# ---- HTTP ----------------------------------------------------------------------


async def _login(client, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_cancel_over_http_returns_the_cancelled_run_with_its_tasks(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-cx1@example.com")
    run_id, _ = await _seed_run(
        db_session_factory,
        status=RunStatus.PAUSED,
        tasks=[
            ("a", "stub_noop_serial", TaskStatus.SUCCEEDED),
            ("b", "stub_noop_serial", TaskStatus.READY),
        ],
    )

    resp = await client.post(f"/api/v1/orchestration/runs/{run_id}/cancel", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled" and body["completed_at"] is not None
    assert {t["plan_key"]: t["status"] for t in body["tasks"]} == {
        "a": "succeeded",
        "b": "cancelled",
    }


async def test_cancel_over_http_is_409_for_a_finished_run_and_404_for_an_unknown_one(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.RISK_MANAGER, "risk-cx2@example.com")
    done, _ = await _seed_run(db_session_factory, status=RunStatus.COMPLETED)

    finished = await client.post(f"/api/v1/orchestration/runs/{done}/cancel", headers=headers)
    unknown = await client.post(
        f"/api/v1/orchestration/runs/{uuid.uuid4()}/cancel", headers=headers
    )

    assert finished.status_code == 409 and "is completed" in finished.json()["detail"]
    assert unknown.status_code == 404
    assert await _run_status(db_session_factory, done) == RunStatus.COMPLETED


async def test_cancel_over_http_is_closed_to_a_read_only_auditor(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "aud-cx3@example.com")
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.RUNNING)

    resp = await client.post(f"/api/v1/orchestration/runs/{run_id}/cancel", headers=headers)
    anonymous = await client.post(f"/api/v1/orchestration/runs/{run_id}/cancel")

    assert resp.status_code == 403 and anonymous.status_code == 401
    assert await _run_status(db_session_factory, run_id) == RunStatus.RUNNING


async def test_a_cancel_is_audited_and_a_refused_cancel_is_not(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-cx4@example.com")
    live, _ = await _seed_run(db_session_factory, status=RunStatus.RUNNING)
    done, _ = await _seed_run(db_session_factory, status=RunStatus.COMPLETED)

    assert (
        await client.post(f"/api/v1/orchestration/runs/{live}/cancel", headers=headers)
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/orchestration/runs/{done}/cancel", headers=headers)
    ).status_code == 409

    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "POST /api/v1/orchestration/runs/{run_id}/cancel"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(r.actor, r.entity_id) for r in rows] == [("admin-cx4@example.com", str(live))]


async def test_http_cancel_then_rerun_gives_a_fresh_completed_run(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-cx5@example.com")
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.PAUSED)
    await client.post(f"/api/v1/orchestration/runs/{run_id}/cancel", headers=headers)

    rerun = await client.post(f"/api/v1/orchestration/runs/{run_id}/rerun", headers=headers)

    assert rerun.status_code == 201
    assert rerun.json()["run_type"] == "rerun" and rerun.json()["status"] == "completed"
    assert rerun.json()["source_run_id"] == str(run_id)


# ---- analytics -----------------------------------------------------------------


async def test_cancelled_tasks_do_not_distort_agent_success_rate_or_duration(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "aud-cx6@example.com")
    run_id, _ = await _seed_run(db_session_factory, status=RunStatus.CANCELLED)
    now = datetime.now(UTC)
    async with db_session_factory() as db:
        plan_id = (
            await db.execute(
                select(OrganizationalPlan.id).where(OrganizationalPlan.run_id == run_id)
            )
        ).scalar_one()
        for key, task_status, seconds in (
            ("ok", TaskStatus.SUCCEEDED, 10),
            ("bad", TaskStatus.FAILED, 30),
            ("cancelled", TaskStatus.CANCELLED, 5000),  # would wreck an average if it counted
        ):
            db.add(
                Task(
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_key=key,
                    capability="graph.ceo_kickoff",
                    name=key,
                    status=task_status,
                    created_at=now - timedelta(seconds=seconds),
                    completed_at=now,
                )
            )
        await db.commit()

    rows = (await client.get("/api/v1/agents/analytics/summary", headers=headers)).json()
    ceo = next(r for r in rows if r["agent_id"] == "ceo-agent")

    assert ceo["tasks_total"] == 3  # it was assigned...
    assert ceo["tasks_succeeded"] == 1 and ceo["tasks_failed"] == 1
    assert ceo["success_rate"] == 0.5  # ...but a cancel is not an outcome
    assert 15 < ceo["avg_duration_seconds"] < 25  # (10 + 30) / 2, not dragged up by 5000
