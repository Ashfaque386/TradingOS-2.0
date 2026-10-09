"""Live per-step agent feed (Phase 19 Finding #1): `PipelineEventSink` hooks in
`src.agents.graph`, the `PipelineEventRecorder` that persists + publishes
them, and the HTTP routes that expose them.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from src.agents import graph as graph_module
from src.agents.graph import _summarize_output, run_pipeline
from src.core.roles import Role
from src.gateway.roster import ROSTER_IDS
from src.orchestration.pipeline_events import (
    PipelineEventRecorder,
    channel_for,
    list_pipeline_events,
)


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, str | None, dict[str, Any] | None]] = []

    async def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((event_type, node, agent_id, payload))

    def types(self) -> list[str]:
        return [e[0] for e in self.events]


# ---- _summarize_output ------------------------------------------------------


def test_summarize_output_keeps_scalars_and_drops_node_log():
    out = _summarize_output(
        {
            "node_log": ["a", "b"],
            "compliance_verdict": {"approved": True, "score": 3, "note": None},
            "validation_attempts": 2,
        }
    )
    assert "node_log" not in out
    assert out["compliance_verdict"] == {"approved": True, "score": 3, "note": None}
    assert out["validation_attempts"] == 2


def test_summarize_output_bounds_long_strings_and_drops_nested_objects():
    out = _summarize_output(
        {
            "generated_code": "x" * 5000,
            "strategy": {"name": "n", "nested": {"deep": 1}, "legs": [1, 2]},
        }
    )
    assert out["generated_code"]["chars"] == 5000
    assert len(out["generated_code"]["preview"]) < 400
    assert out["strategy"] == {"name": "n"}


# ---- graph hooks ------------------------------------------------------------


async def test_happy_path_emits_a_paired_started_completed_for_every_step():
    sink = _CollectingSink()
    state = await run_pipeline("Build a momentum strategy", event_sink=sink)

    assert sink.types()[0] == "pipeline.started"
    assert sink.types()[-1] == "pipeline.completed"

    steps = [e for e in sink.events if e[0].startswith("step.")]
    # Started/completed strictly alternate for the same node, never interleave.
    for started, completed in zip(steps[0::2], steps[1::2], strict=True):
        assert started[0] == "step.started"
        assert completed[0] == "step.completed"
        assert started[1] == completed[1]
        assert started[2] == completed[2]
    # One completed step per entry the pipeline itself logged.
    assert sum(1 for e in sink.events if e[0] == "step.completed") == len(state.node_log)


async def test_step_payloads_carry_index_duration_and_a_bounded_output_summary():
    sink = _CollectingSink()
    await run_pipeline("obj", event_sink=sink)

    completed = [e for e in sink.events if e[0] == "step.completed"]
    assert [e[3]["step_index"] for e in completed] == list(range(len(completed)))
    for _, _, _, payload in completed:
        assert payload["duration_ms"] >= 0
        assert isinstance(payload["output"], dict)

    # An LLM-backed node reports whether the LLM or the deterministic
    # fallback answered -- this sandbox has no provider keys, so: fallback.
    ceo = next(e for e in completed if e[1] == "ceo_kickoff")
    assert ceo[3]["output"]["ceo_brief"]["source"] == "fallback"


async def test_a_disabled_agent_emits_step_skipped_and_never_started():
    sink = _CollectingSink()
    enabled = frozenset(ROSTER_IDS - {"market-analyst"})
    await run_pipeline("obj", enabled_agents=enabled, event_sink=sink)

    skipped = [e for e in sink.events if e[0] == "step.skipped"]
    assert [(e[1], e[2]) for e in skipped] == [("market_analysis_step", "market-analyst")]
    assert skipped[0][3]["reason"] == "agent-disabled"
    assert not any(e[0] == "step.started" and e[1] == "market_analysis_step" for e in sink.events)


async def test_a_failing_node_emits_step_failed_then_pipeline_failed_and_still_raises(
    monkeypatch,
):
    async def _boom(state, router):
        raise RuntimeError("node exploded")

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _boom)
    sink = _CollectingSink()

    with pytest.raises(RuntimeError, match="node exploded"):
        await run_pipeline("obj", event_sink=sink)

    failed = [e for e in sink.events if e[0] == "step.failed"]
    assert len(failed) == 1
    assert failed[0][1] == "market_analysis_step"
    assert "RuntimeError: node exploded" in failed[0][3]["error"]
    assert sink.types()[-1] == "pipeline.failed"


async def test_a_sink_that_raises_never_breaks_the_pipeline():
    class _BrokenSink:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("db gone")

    state = await run_pipeline("obj", event_sink=_BrokenSink())

    assert state.node_log[-1] == "deployment"


async def test_no_sink_is_the_unchanged_default():
    state = await run_pipeline("obj")
    assert state.node_log[-1] == "deployment"


# ---- recorder: persistence + pub/sub ----------------------------------------


async def test_recorder_persists_a_gap_free_ordered_sequence(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)

    await run_pipeline("obj", run_id=str(run_id), event_sink=recorder)

    async with db_session_factory() as db:
        events = await list_pipeline_events(db, run_id=run_id)

    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "pipeline.started"
    assert events[-1].event_type == "pipeline.completed"
    assert {e.run_id for e in events} == {run_id}


async def test_recorder_publishes_each_event_on_the_runs_redis_channel(
    db_session_factory, redis_client
):
    run_id = uuid.uuid4()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_for(run_id))
    await pubsub.get_message(timeout=1)  # subscription confirmation

    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)
    await recorder.emit("step.started", node="ceo_kickoff", agent_id="ceo-agent", payload={"x": 1})

    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message:
            break
        await asyncio.sleep(0.05)
    await pubsub.unsubscribe(channel_for(run_id))
    await pubsub.aclose()  # type: ignore[no-untyped-call]

    assert message is not None
    body = json.loads(message["data"])
    assert body["run_id"] == str(run_id)
    assert body["sequence"] == 1
    assert body["event_type"] == "step.started"
    assert body["node"] == "ceo_kickoff"
    assert body["payload"] == {"x": 1}


async def test_recorder_survives_a_broken_database_and_still_publishes(redis_client):
    class _BrokenFactory:
        def __call__(self) -> Any:
            raise ConnectionError("db gone")

    run_id = uuid.uuid4()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_for(run_id))
    await pubsub.get_message(timeout=1)

    recorder = PipelineEventRecorder(_BrokenFactory(), redis_client, run_id)  # type: ignore[arg-type]
    await recorder.emit("pipeline.started")  # must not raise

    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message:
            break
        await asyncio.sleep(0.05)
    await pubsub.unsubscribe(channel_for(run_id))
    await pubsub.aclose()  # type: ignore[no-untyped-call]

    assert message is not None
    assert json.loads(message["data"])["event_type"] == "pipeline.started"


async def test_list_without_run_id_returns_the_most_recent_events_chronologically(
    db_session_factory,
):
    older, newer = uuid.uuid4(), uuid.uuid4()
    await PipelineEventRecorder(db_session_factory, None, older).emit("pipeline.started")
    newer_recorder = PipelineEventRecorder(db_session_factory, None, newer)
    await newer_recorder.emit("pipeline.started")
    await newer_recorder.emit("pipeline.completed")

    async with db_session_factory() as db:
        events = await list_pipeline_events(db, limit=2)

    # The latest two rows, oldest first -- never the older run's row.
    assert len(events) == 2
    assert all(e.run_id == newer for e in events)
    assert [e.event_type for e in events] == ["pipeline.started", "pipeline.completed"]


# ---- HTTP routes ------------------------------------------------------------


async def _login(client: AsyncClient, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_pipeline_run_returns_a_run_id_whose_steps_can_be_replayed(client, make_user):
    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-events@example.com")

    run = await client.post(
        "/api/v1/agents/pipeline/run", json={"objective": "momentum on NIFTY"}, headers=headers
    )
    assert run.status_code == 200
    run_id = run.json()["run_id"]
    uuid.UUID(run_id)  # a real uuid

    events = await client.get(f"/api/v1/agents/pipeline/events?run_id={run_id}", headers=headers)
    assert events.status_code == 200
    body = events.json()
    assert body[0]["event_type"] == "pipeline.started"
    assert body[-1]["event_type"] == "pipeline.completed"
    assert [e["sequence"] for e in body] == list(range(1, len(body) + 1))
    assert {e["run_id"] for e in body} == {run_id}


async def test_pipeline_events_without_run_id_returns_recent_events(client, make_user):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-events@example.com")
    run = await client.post(
        "/api/v1/agents/pipeline/run", json={"objective": "obj"}, headers=headers
    )

    recent = await client.get("/api/v1/agents/pipeline/events?limit=500", headers=headers)

    assert recent.status_code == 200
    assert run.json()["run_id"] in {e["run_id"] for e in recent.json()}


async def test_pipeline_events_are_readable_by_every_role_but_running_is_not(client, make_user):
    auditor = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "auditor-events@example.com")

    read = await client.get("/api/v1/agents/pipeline/events", headers=auditor)
    run = await client.post("/api/v1/agents/pipeline/run", json={"objective": "o"}, headers=auditor)

    assert read.status_code == 200
    assert run.status_code == 403


async def test_pipeline_events_rejects_an_unauthenticated_caller(client):
    resp = await client.get("/api/v1/agents/pipeline/events")
    assert resp.status_code == 401


async def test_pipeline_events_limit_is_validated(client, make_user):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-limit@example.com")
    assert (
        await client.get("/api/v1/agents/pipeline/events?limit=0", headers=headers)
    ).status_code == 422
    assert (
        await client.get("/api/v1/agents/pipeline/events?limit=501", headers=headers)
    ).status_code == 422


async def test_unknown_run_id_returns_an_empty_list_not_an_error(client, make_user):
    headers = await _login(
        client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-unknown@example.com"
    )
    resp = await client.get(
        f"/api/v1/agents/pipeline/events?run_id={uuid.uuid4()}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == []
