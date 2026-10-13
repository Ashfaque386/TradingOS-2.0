import asyncio
import uuid

import pytest
from sqlalchemy import select

from src.models.audit_log import AuditLog
from src.models.organization_run import FailureClass, OrganizationRun, RunSource, RunStatus, RunType
from src.orchestration import run_control
from src.orchestration.run_control import PermanentFailureRetryError


async def _make_run(
    db_session_factory, *, status: RunStatus, failure_class=None, run_type=RunType.STANDARD
):
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=run_type,
            status=status,
            failure_class=failure_class,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def test_pause_run_race_safety_only_one_of_two_concurrent_calls_wins(
    db_session_factory, redis_client
):
    run_id = await _make_run(db_session_factory, status=RunStatus.RUNNING)

    async def _pause() -> bool:
        async with db_session_factory() as db:
            return await run_control.pause_run(db, redis_client, run_id)

    results = await asyncio.gather(_pause(), _pause())
    assert sorted(results) == [False, True]

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.PAUSED


async def test_retry_run_race_safety_only_one_of_two_concurrent_calls_wins(
    db_session_factory, redis_client
):
    run_id = await _make_run(
        db_session_factory, status=RunStatus.FAILED, failure_class=FailureClass.TRANSIENT
    )

    async def _retry() -> bool:
        return await run_control.retry_run(db_session_factory, redis_client, run_id)

    results = await asyncio.gather(_retry(), _retry())
    assert sorted(results) == [False, True]

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        # Both attempts race for the FAILED->RUNNING transition; whichever
        # wins then drives the run (no tasks exist, so it immediately
        # completes) -- either way the run must land in exactly one
        # consistent terminal-or-running state, never something corrupted.
        assert run.status in (RunStatus.RUNNING, RunStatus.COMPLETED)


async def test_retry_run_rejects_permanent_failure(db_session_factory, redis_client):
    run_id = await _make_run(
        db_session_factory, status=RunStatus.FAILED, failure_class=FailureClass.PERMANENT
    )

    with pytest.raises(PermanentFailureRetryError):
        await run_control.retry_run(db_session_factory, redis_client, run_id)

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.FAILED


async def test_retry_run_returns_false_when_not_failed(db_session_factory, redis_client):
    run_id = await _make_run(db_session_factory, status=RunStatus.RUNNING)
    applied = await run_control.retry_run(db_session_factory, redis_client, run_id)
    assert applied is False


@pytest.mark.parametrize("source_run_type", [RunType.STANDARD, RunType.RERUN])
async def test_rerun_never_inherits_run_type(db_session_factory, redis_client, source_run_type):
    source_id = await _make_run(
        db_session_factory, status=RunStatus.COMPLETED, run_type=source_run_type
    )

    new_run = await run_control.rerun_run(db_session_factory, redis_client, source_id)

    assert new_run is not None
    assert new_run.run_type == RunType.RERUN
    assert new_run.source_run_id == source_id
    assert new_run.id != source_id


async def test_rerun_of_unknown_run_returns_none(db_session_factory, redis_client):
    result = await run_control.rerun_run(db_session_factory, redis_client, uuid.uuid4())
    assert result is None


# ---- continue + the HTTP endpoints (backlog item 21 audit) ----------------------
# Pause, continue, retry and rerun were all built in Phase 2/19, but only rerun
# had an HTTP-level test and `continue_run` had none at all.


async def test_continue_run_resumes_a_paused_run_and_drives_it_to_completion(
    db_session_factory, redis_client
):
    run_id = await _make_run(db_session_factory, status=RunStatus.PAUSED)

    assert await run_control.continue_run(db_session_factory, redis_client, run_id) is True

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.COMPLETED  # no tasks, so it drains immediately


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED])
async def test_continue_run_only_applies_to_a_paused_run(db_session_factory, redis_client, status):
    run_id = await _make_run(db_session_factory, status=status)

    assert await run_control.continue_run(db_session_factory, redis_client, run_id) is False

    async with db_session_factory() as db:
        assert (await db.get(OrganizationRun, run_id)).status == status


async def _login(client, make_user, role, email):
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_pause_then_continue_over_http(client, make_user, db_session_factory):
    from src.core.roles import Role

    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-rc1@example.com")
    run_id = await _make_run(db_session_factory, status=RunStatus.RUNNING)

    paused = await client.post(f"/api/v1/orchestration/runs/{run_id}/pause", headers=headers)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"

    resumed = await client.post(f"/api/v1/orchestration/runs/{run_id}/continue", headers=headers)
    assert resumed.status_code == 200 and resumed.json()["status"] == "completed"


@pytest.mark.parametrize(
    ("action", "status", "failure_class", "needs"),
    [
        ("pause", RunStatus.COMPLETED, None, "a running run can be paused"),
        ("pause", RunStatus.PAUSED, None, "a running run can be paused"),
        ("continue", RunStatus.RUNNING, None, "a paused run can be continued"),
        ("continue", RunStatus.COMPLETED, None, "a paused run can be continued"),
        ("retry", RunStatus.COMPLETED, None, "a failed run can be retried"),
        ("retry", RunStatus.RUNNING, None, "a failed run can be retried"),
    ],
)
async def test_a_control_that_would_change_nothing_is_refused_not_answered_200(
    client, make_user, db_session_factory, action, status, failure_class, needs
):
    """A 200 no-op would be audited (the middleware writes a row for every 2xx):
    a "paused" entry in the permanent trail for a pause that never happened."""
    from src.core.roles import Role

    email = f"admin-noop-{action}-{status.value}@example.com"
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, email)
    run_id = await _make_run(db_session_factory, status=status, failure_class=failure_class)

    resp = await client.post(f"/api/v1/orchestration/runs/{run_id}/{action}", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == f"This run is {status.value}; only {needs}"
    async with db_session_factory() as db:
        assert (await db.get(OrganizationRun, run_id)).status == status  # untouched


async def test_only_controls_that_took_effect_are_audited(client, make_user, db_session_factory):
    from src.core.roles import Role

    email = "admin-audit-rc@example.com"
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, email)
    running = await _make_run(db_session_factory, status=RunStatus.RUNNING)
    failed = await _make_run(
        db_session_factory, status=RunStatus.FAILED, failure_class=FailureClass.TRANSIENT
    )
    base = "/api/v1/orchestration/runs"

    # Each action once where it applies, then again where it no longer does.
    assert (await client.post(f"{base}/{running}/pause", headers=headers)).status_code == 200
    assert (await client.post(f"{base}/{running}/pause", headers=headers)).status_code == 409
    assert (await client.post(f"{base}/{running}/continue", headers=headers)).status_code == 200
    assert (await client.post(f"{base}/{running}/continue", headers=headers)).status_code == 409
    assert (await client.post(f"{base}/{failed}/retry", headers=headers)).status_code == 200
    assert (await client.post(f"{base}/{failed}/retry", headers=headers)).status_code == 409

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()
    trail = sorted(
        (r.action.removeprefix("POST /api/v1/orchestration/runs/{run_id}/"), r.entity_id)
        for r in rows
        if r.action.startswith("POST /api/v1/orchestration/runs/{run_id}/")
        and r.entity_id in {str(running), str(failed)}
    )
    assert trail == sorted(
        [("pause", str(running)), ("continue", str(running)), ("retry", str(failed))]
    )


async def test_retry_endpoint_retries_a_transient_failure_and_refuses_a_permanent_one(
    client, make_user, db_session_factory
):
    from src.core.roles import Role

    headers = await _login(client, make_user, Role.RISK_MANAGER, "risk-rc3@example.com")
    transient = await _make_run(
        db_session_factory, status=RunStatus.FAILED, failure_class=FailureClass.TRANSIENT
    )
    permanent = await _make_run(
        db_session_factory, status=RunStatus.FAILED, failure_class=FailureClass.PERMANENT
    )

    ok = await client.post(f"/api/v1/orchestration/runs/{transient}/retry", headers=headers)
    assert ok.status_code == 200 and ok.json()["status"] == "completed"

    refused = await client.post(f"/api/v1/orchestration/runs/{permanent}/retry", headers=headers)
    assert refused.status_code == 409
    async with db_session_factory() as db:
        assert (await db.get(OrganizationRun, permanent)).status == RunStatus.FAILED


async def test_run_controls_are_closed_to_a_read_only_auditor(
    client, make_user, db_session_factory
):
    from src.core.roles import Role

    headers = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "aud-rc4@example.com")
    run_id = await _make_run(db_session_factory, status=RunStatus.RUNNING)

    for action in ("pause", "continue", "retry", "rerun"):
        resp = await client.post(f"/api/v1/orchestration/runs/{run_id}/{action}", headers=headers)
        assert resp.status_code == 403, action
    async with db_session_factory() as db:
        assert (await db.get(OrganizationRun, run_id)).status == RunStatus.RUNNING


async def test_run_controls_404_for_an_unknown_run(client, make_user):
    from src.core.roles import Role

    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-rc5@example.com")
    unknown = "00000000-0000-0000-0000-000000000000"

    for action in ("pause", "continue", "retry"):
        resp = await client.post(f"/api/v1/orchestration/runs/{unknown}/{action}", headers=headers)
        assert resp.status_code == 404, action
        assert resp.json()["detail"] == "Run not found", action
