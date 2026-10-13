"""A pipeline run whose process died must not read "running" for ever.

A LangGraph pipeline runs inside the HTTP request that started it. When that
process dies -- a crash, a redeploy, an OOM kill -- nothing is left to write an
ending: the event log just stops, the feed shows a spinner that never resolves,
and a cancel was accepted (202, an audit row) and never consumed. These pin the
three parts of the fix: a heartbeat while a run executes, a state derived from it
(`abandoned`), and a reaper that gives the run the ending it could not write.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.agents import graph as graph_module
from src.agents.state import TradingOSGraphState
from src.core.roles import Role
from src.models.agent_pipeline_event import AgentPipelineEvent
from src.orchestration import pipeline_events as pe
from src.orchestration.pipeline_events import (
    ABANDONED_AFTER_SECONDS,
    ABANDONED_AFTER_SECONDS_WITHOUT_LIVENESS,
    PipelineEventRecorder,
    alive_key_for,
    channel_for,
    pipeline_run_state,
    reap_abandoned_runs,
)


async def _seed(
    session_factory,
    run_id: uuid.UUID,
    types: list[tuple[str, str | None]],
    *,
    age_seconds: float,
) -> None:
    """A run's events as they sit in the database, the last one `age_seconds` old."""
    last = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with session_factory() as db:
        for i, (event_type, node) in enumerate(types, start=1):
            db.add(
                AgentPipelineEvent(
                    run_id=run_id,
                    sequence=i,
                    event_type=event_type,
                    node=node,
                    payload=None,
                    created_at=last - timedelta(seconds=len(types) - i),
                )
            )
        await db.commit()


_MID_RUN = [
    ("pipeline.started", None),
    ("step.started", "ceo_kickoff"),
    ("step.completed", "ceo_kickoff"),
    ("step.started", "market_analysis_step"),
]


async def _state(session_factory, run_id, redis=None) -> str:
    async with session_factory() as db:
        return await pipeline_run_state(db, run_id, redis)


async def _events(session_factory, run_id) -> list[AgentPipelineEvent]:
    async with session_factory() as db:
        return await pe.list_pipeline_events(db, run_id=run_id)


class _BrokenRedis:
    async def exists(self, *_: Any) -> int:
        raise ConnectionError("redis is down")


# ---- the state ----------------------------------------------------------------------


async def test_a_recent_run_with_no_heartbeat_yet_is_still_running(
    db_session_factory, redis_client
):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=5)

    assert await _state(db_session_factory, run_id, redis_client) == "running"


async def test_a_quiet_run_whose_process_is_alive_is_running(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 5)
    await redis_client.set(alive_key_for(run_id), "1", ex=30)  # a long LLM call, still beating
    try:
        assert await _state(db_session_factory, run_id, redis_client) == "running"
    finally:
        await redis_client.delete(alive_key_for(run_id))


async def test_a_quiet_run_with_no_heartbeat_is_abandoned(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 2)

    assert await _state(db_session_factory, run_id, redis_client) == "abandoned"


async def test_a_finished_run_stays_finished_however_old(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    await _seed(
        db_session_factory,
        run_id,
        [*_MID_RUN, ("pipeline.completed", None)],
        age_seconds=86_400,
    )

    assert await _state(db_session_factory, run_id, redis_client) == "finished"


async def test_without_redis_only_long_silence_counts_as_abandoned(db_session_factory):
    """No heartbeat to read, so the only evidence is silence -- and an LLM call
    can take minutes, so it must be a lot more of it."""
    quiet, dead = uuid.uuid4(), uuid.uuid4()
    await _seed(db_session_factory, quiet, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 3)
    await _seed(
        db_session_factory, dead, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS_WITHOUT_LIVENESS * 2
    )

    assert await _state(db_session_factory, quiet, None) == "running"
    assert await _state(db_session_factory, dead, None) == "abandoned"


async def test_an_unreadable_redis_falls_back_to_the_weaker_rule(db_session_factory):
    quiet = uuid.uuid4()
    await _seed(db_session_factory, quiet, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 3)

    assert await _state(db_session_factory, quiet, _BrokenRedis()) == "running"


# ---- the heartbeat -------------------------------------------------------------------


async def test_the_heartbeat_exists_before_the_run_and_is_gone_after(
    db_session_factory, redis_client
):
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)
    key = alive_key_for(run_id)

    assert not await redis_client.exists(key)
    async with recorder.heartbeat():
        assert await redis_client.exists(key)
        assert 0 < await redis_client.ttl(key) <= pe.ALIVE_TTL_SECONDS
    assert not await redis_client.exists(key)


async def test_the_heartbeat_keeps_refreshing_while_the_run_lasts_and_stops_after(
    db_session_factory, redis_client, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pe, "ALIVE_INTERVAL_SECONDS", 0.05)
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)
    key = alive_key_for(run_id)

    async with recorder.heartbeat():
        await redis_client.delete(key)  # as if it had expired
        await asyncio.sleep(0.3)
        assert await redis_client.exists(key)  # the beat put it back
    await asyncio.sleep(0.3)
    assert not await redis_client.exists(key)  # and nothing keeps beating for a finished run


async def test_the_heartbeat_is_cleared_when_the_run_raises(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)

    with pytest.raises(RuntimeError):
        async with recorder.heartbeat():
            raise RuntimeError("the run failed")

    assert not await redis_client.exists(alive_key_for(run_id))


async def test_a_recorder_with_no_redis_runs_its_block_unchanged(db_session_factory):
    recorder = PipelineEventRecorder(db_session_factory, None, uuid.uuid4())
    ran = False

    async with recorder.heartbeat():
        ran = True

    assert ran


# ---- the reaper ----------------------------------------------------------------------


async def test_the_reaper_ends_a_dead_run_with_an_event_that_says_what_happened(
    db_session_factory, redis_client
):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 2)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_for(run_id))

    reaped = await reap_abandoned_runs(db_session_factory, redis_client)

    assert reaped == [run_id]
    events = await _events(db_session_factory, run_id)
    last = events[-1]
    assert last.event_type == "pipeline.abandoned"
    assert last.sequence == len(_MID_RUN) + 1  # continues the log, gap-free
    assert last.node == "market_analysis_step"  # where it stopped
    assert last.payload is not None
    assert last.payload["last_event"] == "step.started"
    assert last.payload["steps_completed"] == 1
    assert "process" in last.payload["reason"]
    assert await _state(db_session_factory, run_id, redis_client) == "finished"

    messages = [m async for m in _drain(pubsub)]
    assert any("pipeline.abandoned" in str(m["data"]) for m in messages), "the feed was not told"
    await pubsub.aclose()


async def _drain(pubsub, *, seconds: float = 2.0):
    """Every message that arrives within `seconds` (the first reads can be the
    subscribe confirmation, so a single empty read does not mean "nothing")."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message is not None:
            yield message


async def test_the_reaper_is_idempotent(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 2)

    assert await reap_abandoned_runs(db_session_factory, redis_client) == [run_id]
    assert await reap_abandoned_runs(db_session_factory, redis_client) == []
    assert [e.event_type for e in await _events(db_session_factory, run_id)].count(
        "pipeline.abandoned"
    ) == 1


async def test_the_reaper_leaves_alive_young_and_finished_runs_alone(
    db_session_factory, redis_client
):
    alive, young, finished = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old = ABANDONED_AFTER_SECONDS * 5
    await _seed(db_session_factory, alive, _MID_RUN, age_seconds=old)
    await _seed(db_session_factory, young, _MID_RUN, age_seconds=5)
    await _seed(
        db_session_factory, finished, [*_MID_RUN, ("pipeline.failed", None)], age_seconds=old
    )
    await redis_client.set(alive_key_for(alive), "1", ex=30)  # e.g. a run on another instance
    try:
        assert await reap_abandoned_runs(db_session_factory, redis_client) == []
    finally:
        await redis_client.delete(alive_key_for(alive))

    for run_id in (alive, young, finished):
        assert not any(
            e.event_type == "pipeline.abandoned" for e in await _events(db_session_factory, run_id)
        )


async def test_the_reaper_does_not_claim_a_run_it_could_not_end(
    db_session_factory, redis_client, monkeypatch: pytest.MonkeyPatch
):
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 2)

    async def _fails(self, *args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(PipelineEventRecorder, "emit_checked", _fails)

    assert await reap_abandoned_runs(db_session_factory, redis_client) == []


# ---- HTTP ------------------------------------------------------------------------------


async def _login(client, make_user, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_cancelling_an_abandoned_run_is_refused_not_accepted(
    client, make_user, db_session_factory, redis_client
):
    headers = await _login(client, make_user, "admin-ab1@example.com")
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 2)

    response = await client.post(f"/api/v1/agents/pipeline/runs/{run_id}/cancel", headers=headers)

    assert response.status_code == 409
    assert "no longer running" in response.json()["detail"]
    assert not await redis_client.exists(pe.cancel_key_for(run_id)), "a dead run got a cancel flag"


async def test_cancelling_a_quiet_but_alive_run_is_still_accepted(
    client, make_user, db_session_factory, redis_client
):
    headers = await _login(client, make_user, "admin-ab2@example.com")
    run_id = uuid.uuid4()
    await _seed(db_session_factory, run_id, _MID_RUN, age_seconds=ABANDONED_AFTER_SECONDS * 5)
    await redis_client.set(alive_key_for(run_id), "1", ex=30)
    try:
        response = await client.post(
            f"/api/v1/agents/pipeline/runs/{run_id}/cancel", headers=headers
        )
    finally:
        await redis_client.delete(alive_key_for(run_id))
        await pe.clear_cancel(redis_client, run_id)

    assert response.status_code == 202


async def test_a_run_through_the_real_endpoint_is_alive_while_it_executes(
    client, make_user, redis_client, monkeypatch: pytest.MonkeyPatch
):
    """The wiring: the heartbeat must be set while the pipeline runs inside the
    request, and gone once it has returned."""
    headers = await _login(client, make_user, "admin-ab3@example.com")
    seen: dict[str, Any] = {}

    async def _step(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        run_id = state.run_id
        seen["alive_during"] = bool(await redis_client.exists(alive_key_for(run_id or "")))
        return {"market_analysis": {"trend": "neutral"}, "node_log": [*state.node_log, "mine"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _step)

    response = await client.post(
        "/api/v1/agents/pipeline/run", json={"objective": "wiring check"}, headers=headers
    )

    assert response.status_code == 200
    assert seen["alive_during"] is True
    assert not await redis_client.exists(alive_key_for(response.json()["run_id"]))
