import asyncio
import uuid

import pytest

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
