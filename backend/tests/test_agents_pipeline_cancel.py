"""Cancelling a LangGraph pipeline run (backlog item 21's companion: the
pipeline is where runs actually take long enough to cancel).

A cancel is honoured at a step boundary and also *during* a step: the running
node (an in-flight LLM call, say) is abandoned and its result discarded. These
tests pin where the run stops, what it reports, what it refuses to record, that
a step which finishes despite the cancel is kept, and that a flaky cancel
channel can never take a healthy run down with it.
"""

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.agents import graph as graph_module
from src.agents.graph import PipelineCancelledError, run_pipeline
from src.agents.roster import PIPELINE_NODE_AGENTS
from src.agents.state import TradingOSGraphState
from src.core.roles import Role
from src.gateway.roster import ROSTER_IDS
from src.models.audit_log import AuditLog
from src.orchestration.pipeline_events import (
    CANCEL_FLAG_TTL_SECONDS,
    PipelineEventRecorder,
    cancel_key_for,
    clear_cancel,
    pipeline_run_state,
    request_cancel,
)


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, Any] | None]] = []

    async def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((event_type, node, payload))

    def types(self) -> list[str]:
        return [e[0] for e in self.events]


class _Signal:
    """Reports 'cancelled' once more than `cancel_after` boundaries have been
    checked, so `cancel_after=3` lets exactly three steps run."""

    def __init__(self, cancel_after: int | None = None, *, raises: bool = False) -> None:
        self.cancel_after = cancel_after
        self.raises = raises
        self.checks = 0

    async def is_cancelled(self) -> bool:
        self.checks += 1
        if self.raises:
            raise RuntimeError("redis is down")
        return self.cancel_after is not None and self.checks > self.cancel_after


class _Memory:
    def __init__(self) -> None:
        self.recalled: list[str] = []
        self.remembered: list[str] = []

    async def recall(self, objective: str) -> list[str]:
        self.recalled.append(objective)
        return []

    async def remember(self, text: str, metadata: dict[str, Any]) -> None:
        self.remembered.append(text)


# ---- the graph: where a cancel takes effect ------------------------------------


async def test_a_cancel_before_the_first_step_stops_the_run_before_anything_executes():
    sink = _Sink()

    with pytest.raises(PipelineCancelledError) as caught:
        await run_pipeline("obj", event_sink=sink, cancel_signal=_Signal(cancel_after=0))

    assert caught.value.node == "ceo_kickoff" and caught.value.step_index == 0
    assert caught.value.state.node_log == []
    assert sink.types() == ["pipeline.started", "pipeline.cancelled"]


async def test_a_cancel_mid_run_stops_at_the_next_boundary_and_keeps_finished_work():
    sink = _Sink()

    with pytest.raises(PipelineCancelledError) as caught:
        await run_pipeline("obj", event_sink=sink, cancel_signal=_Signal(cancel_after=3))

    # Three steps ran; the fourth never started.
    assert caught.value.state.node_log == ["ceo_kickoff", "market_analysis", "strategy_generation"]
    assert caught.value.node == "options_strategy" and caught.value.step_index == 3
    assert sink.types().count("step.completed") == 3
    assert sink.types().count("step.started") == 3
    assert sink.types()[-1] == "pipeline.cancelled"
    assert "pipeline.completed" not in sink.types()
    cancelled = sink.events[-1]
    assert cancelled[1] == "options_strategy"
    assert cancelled[2] == {"step_index": 3, "steps_completed": 3, "in_flight": False}


async def test_a_step_that_finishes_before_the_cancel_is_seen_is_kept(
    monkeypatch: pytest.MonkeyPatch,
):
    signal = _Signal()
    finished: list[str] = []

    async def _step(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        signal.cancel_after = signal.checks  # the cancel lands while this step runs
        await asyncio.sleep(0)
        finished.append("market_analysis_step")
        return {"market_analysis": {"trend": "neutral"}, "node_log": [*state.node_log, "mine"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _step)

    with pytest.raises(PipelineCancelledError) as caught:
        await run_pipeline("obj", cancel_signal=signal)

    assert finished == ["market_analysis_step"]
    assert caught.value.state.node_log[-1] == "mine"  # its result was kept...
    assert caught.value.node == "strategy_generation"  # ...and the *next* step never started
    assert caught.value.in_flight is False


class _Hung:
    """A node that never returns on its own, like an LLM call that has stalled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.interrupted = False
        self.completed = False

    async def __call__(self, state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        self.completed = True
        return {"market_analysis": {"trend": "never"}, "node_log": [*state.node_log, "never"]}


class _CancelWhenStarted:
    """Cancelled as soon as `hung` has begun executing."""

    def __init__(self, hung: _Hung) -> None:
        self.hung = hung

    async def is_cancelled(self) -> bool:
        return self.hung.started.is_set()


async def test_a_cancel_aborts_the_step_that_is_running(monkeypatch: pytest.MonkeyPatch):
    hung = _Hung()
    sink = _Sink()
    monkeypatch.setattr(graph_module, "CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", hung)

    with pytest.raises(PipelineCancelledError) as caught:
        await asyncio.wait_for(
            run_pipeline("obj", event_sink=sink, cancel_signal=_CancelWhenStarted(hung)),
            timeout=10,
        )

    assert hung.interrupted and not hung.completed  # the call was dropped, not waited for
    assert caught.value.in_flight is True
    assert caught.value.node == "market_analysis_step" and caught.value.step_index == 1
    assert caught.value.state.node_log == ["ceo_kickoff"]  # the aborted step is not "done"
    assert sink.types() == [
        "pipeline.started",
        "step.started",
        "step.completed",
        "step.started",
        "step.cancelled",
        "pipeline.cancelled",
    ]
    step_cancelled, ended = sink.events[-2], sink.events[-1]
    assert step_cancelled[1] == "market_analysis_step"
    assert step_cancelled[2] is not None and step_cancelled[2]["step_index"] == 1
    assert ended[2] == {"step_index": 1, "steps_completed": 1, "in_flight": True}
    assert "step.failed" not in sink.types()


async def test_an_aborted_step_is_not_recorded_to_memory(monkeypatch: pytest.MonkeyPatch):
    hung = _Hung()
    memory = _Memory()
    monkeypatch.setattr(graph_module, "CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", hung)

    with pytest.raises(PipelineCancelledError):
        await asyncio.wait_for(
            run_pipeline("obj", memory=memory, cancel_signal=_CancelWhenStarted(hung)), timeout=10
        )

    assert memory.remembered == []


async def test_a_step_that_ignores_the_abort_and_finishes_keeps_its_result(
    monkeypatch: pytest.MonkeyPatch,
):
    """The step is not actually stopped by the cancel (it finishes anyway): its
    finished work is kept, not thrown away. Pinned on `_run_step` directly, since
    this is the race a real cancel can lose by a hair."""
    monkeypatch.setattr(graph_module, "CANCEL_POLL_SECONDS", 0.01)
    started = asyncio.Event()

    async def _stubborn(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass  # swallows the abort and returns normally
        return {"node_log": ["finished anyway"]}

    class _Signal2:
        async def is_cancelled(self) -> bool:
            return started.is_set()

    result = await asyncio.wait_for(
        graph_module._run_step(_stubborn, TradingOSGraphState(objective="o"), None, _Signal2()),  # type: ignore[arg-type]
        timeout=10,
    )

    assert result == {"node_log": ["finished anyway"]}


async def test_a_step_that_fails_with_a_cancel_pending_but_unseen_still_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """A genuine error is a failure, not a cancel: the polling must not mask it."""
    sink = _Sink()

    async def _boom(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        raise ValueError("node exploded")

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _boom)

    with pytest.raises(ValueError, match="node exploded"):
        await run_pipeline("obj", event_sink=sink, cancel_signal=_Signal())

    assert "step.failed" in sink.types() and "step.cancelled" not in sink.types()
    assert sink.types()[-1] == "pipeline.failed"


async def test_a_run_cancelled_from_outside_does_not_leak_its_step(
    monkeypatch: pytest.MonkeyPatch,
):
    """Shutdown or a dropped client cancels the run's own task; the node it was
    running must not keep going (an orphaned LLM call) afterwards."""
    hung = _Hung()
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", hung)

    runner = asyncio.create_task(run_pipeline("obj", cancel_signal=_Signal()))
    await asyncio.wait_for(hung.started.wait(), timeout=10)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    await asyncio.sleep(0.05)

    assert hung.interrupted


async def test_a_disabled_agents_skipped_step_is_also_a_checkpoint():
    sink = _Sink()
    skipped_agent = dict(PIPELINE_NODE_AGENTS)["market_analysis_step"]

    with pytest.raises(PipelineCancelledError) as caught:
        await run_pipeline(
            "obj",
            enabled_agents=ROSTER_IDS - {skipped_agent},
            event_sink=sink,
            cancel_signal=_Signal(cancel_after=1),
        )

    assert caught.value.node == "market_analysis_step"
    assert "step.skipped" not in sink.types()


async def test_nothing_is_recorded_to_memory_for_a_cancelled_run():
    memory = _Memory()

    with pytest.raises(PipelineCancelledError):
        await run_pipeline("obj", memory=memory, cancel_signal=_Signal(cancel_after=2))

    assert memory.recalled == ["obj"]  # reading memory up front is fine
    assert memory.remembered == []  # a cancelled run is not a lesson


async def test_an_unreadable_cancel_signal_never_breaks_a_healthy_run():
    state = await run_pipeline("obj", cancel_signal=_Signal(raises=True))

    assert state.deployment_result is not None


async def test_without_a_cancel_signal_the_pipeline_is_unchanged():
    sink = _Sink()
    state = await run_pipeline("obj", event_sink=sink)

    assert state.deployment_result is not None
    assert sink.types()[-1] == "pipeline.completed"


# ---- the Redis flag + the run-state query --------------------------------------


async def test_the_recorder_reads_the_redis_flag_fresh_each_time(db_session_factory, redis_client):
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(db_session_factory, redis_client, run_id)

    assert await recorder.is_cancelled() is False
    await request_cancel(redis_client, run_id)
    assert await recorder.is_cancelled() is True
    assert 0 < await redis_client.ttl(cancel_key_for(run_id)) <= CANCEL_FLAG_TTL_SECONDS
    await clear_cancel(redis_client, run_id)
    assert await recorder.is_cancelled() is False


async def test_a_flag_for_one_run_does_not_cancel_another(db_session_factory, redis_client):
    mine, other = uuid.uuid4(), uuid.uuid4()
    await request_cancel(redis_client, other)
    try:
        assert (
            await PipelineEventRecorder(db_session_factory, redis_client, mine).is_cancelled()
            is False
        )
    finally:
        await clear_cancel(redis_client, other)


async def test_a_recorder_with_no_redis_is_never_cancelled(db_session_factory):
    assert (
        await PipelineEventRecorder(db_session_factory, None, uuid.uuid4()).is_cancelled() is False
    )


async def test_run_state_is_derived_from_the_durable_event_log(db_session_factory):
    async def _state(run_id: uuid.UUID) -> str:
        async with db_session_factory() as db:
            return await pipeline_run_state(db, run_id)

    unknown, running, completed, cancelled = (uuid.uuid4() for _ in range(4))
    assert await _state(unknown) == "unknown"

    for run_id, terminal in (
        (running, None),
        (completed, "pipeline.completed"),
        (cancelled, "pipeline.cancelled"),
    ):
        recorder = PipelineEventRecorder(db_session_factory, None, run_id)
        await recorder.emit("pipeline.started")
        await recorder.emit("step.started", node="ceo_kickoff")
        if terminal:
            await recorder.emit(terminal)

    assert await _state(running) == "running"
    assert await _state(completed) == "finished"
    assert await _state(cancelled) == "finished"


# ---- HTTP: cancelling a run that is genuinely in flight -------------------------


async def _login(client: AsyncClient, make_user: Any, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class _InFlightRun:
    """A pipeline run held open at its second step by an asyncio gate, so a
    cancel can arrive on a different request while it is truly running."""

    def __init__(self, client: AsyncClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None
        self.run_id = ""

    async def step(self, state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        self.entered.set()
        await self.gate.wait()
        return {
            "market_analysis": {"trend": "neutral"},
            "node_log": [*state.node_log, "market_analysis"],
        }

    async def start(self) -> None:
        self.task = asyncio.create_task(
            self.client.post(
                "/api/v1/agents/pipeline/run", json={"objective": "cancel me"}, headers=self.headers
            )
        )
        await asyncio.wait_for(self.entered.wait(), timeout=15)
        events = await self.client.get(
            "/api/v1/agents/pipeline/events?limit=20", headers=self.headers
        )
        self.run_id = events.json()[-1]["run_id"]

    async def finish(self) -> Any:
        self.gate.set()
        assert self.task is not None
        return await asyncio.wait_for(self.task, timeout=30)


@pytest.fixture
def in_flight(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    runs: list[_InFlightRun] = []

    def _make(headers: dict[str, str]) -> _InFlightRun:
        run = _InFlightRun(client, headers)
        monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", run.step)
        runs.append(run)
        return run

    yield _make
    for run in runs:  # never leave a run hanging on its gate if a test failed early
        run.gate.set()
        if run.task is not None and not run.task.done():
            run.task.cancel()


async def test_cancelling_a_running_pipeline_aborts_the_step_in_progress(
    client, make_user, in_flight, redis_client
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc1@example.com")
    run = in_flight(headers)
    await run.start()

    cancel = await client.post(f"/api/v1/agents/pipeline/runs/{run.run_id}/cancel", headers=headers)
    assert cancel.status_code == 202
    assert cancel.json() == {"run_id": run.run_id, "status": "cancel_requested"}

    # Not released: the step is stuck on its gate, and the cancel gets through anyway.
    assert run.task is not None
    response = await asyncio.wait_for(run.task, timeout=15)
    assert response.status_code == 200
    body = response.json()
    assert body["cancelled"] is True and body["run_id"] == run.run_id
    assert body["node_log"] == ["ceo_kickoff"]  # the aborted step is not counted as done
    assert body["deployment_result"] is None

    events = (
        await client.get(f"/api/v1/agents/pipeline/events?run_id={run.run_id}", headers=headers)
    ).json()
    types = [e["event_type"] for e in events]
    assert types[-2:] == ["step.cancelled", "pipeline.cancelled"]
    assert "pipeline.completed" not in types
    assert events[-2]["node"] == events[-1]["node"] == "market_analysis_step"
    assert events[-1]["payload"] == {"step_index": 1, "steps_completed": 1, "in_flight": True}
    assert await redis_client.exists(cancel_key_for(run.run_id)) == 0  # flag cleaned up


async def test_a_run_that_is_not_cancelled_completes_normally(client, make_user, in_flight):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc2@example.com")
    run = in_flight(headers)
    await run.start()

    response = await run.finish()

    assert response.json()["cancelled"] is False
    assert response.json()["deployment_result"] is not None


async def test_cancelling_twice_is_accepted_both_times(client, make_user, in_flight):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc3@example.com")
    run = in_flight(headers)
    await run.start()
    url = f"/api/v1/agents/pipeline/runs/{run.run_id}/cancel"

    assert (await client.post(url, headers=headers)).status_code == 202
    assert (await client.post(url, headers=headers)).status_code == 202

    assert (await run.finish()).json()["cancelled"] is True


async def test_a_portfolio_manager_can_cancel_but_other_roles_cannot(client, make_user, in_flight):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc4@example.com")
    pm = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-pc4@example.com")
    risk = await _login(client, make_user, Role.RISK_MANAGER, "risk-pc4@example.com")
    auditor = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "aud-pc4@example.com")
    run = in_flight(admin)
    await run.start()
    url = f"/api/v1/agents/pipeline/runs/{run.run_id}/cancel"

    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers=risk)).status_code == 403
    assert (await client.post(url, headers=auditor)).status_code == 403
    # Refused callers did not cancel anything: the run is still going.
    assert run.task is not None and not run.task.done()
    assert (await client.post(url, headers=pm)).status_code == 202

    assert (await run.finish()).json()["cancelled"] is True


async def test_cancelling_an_unknown_or_finished_run_is_refused(client, make_user):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc5@example.com")
    done = (
        await client.post(
            "/api/v1/agents/pipeline/run", json={"objective": "quick"}, headers=headers
        )
    ).json()["run_id"]

    unknown = await client.post(
        f"/api/v1/agents/pipeline/runs/{uuid.uuid4()}/cancel", headers=headers
    )
    finished = await client.post(f"/api/v1/agents/pipeline/runs/{done}/cancel", headers=headers)

    assert unknown.status_code == 404
    assert finished.status_code == 409 and "already finished" in finished.json()["detail"]


async def test_a_cancel_is_audited_and_a_refused_cancel_is_not(
    client, make_user, in_flight, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc6@example.com")
    run = in_flight(headers)
    await run.start()
    url = f"/api/v1/agents/pipeline/runs/{run.run_id}/cancel"

    assert (await client.post(url, headers=headers)).status_code == 202
    await run.finish()
    assert (await client.post(url, headers=headers)).status_code == 409  # now finished

    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "POST /api/v1/agents/pipeline/runs/{run_id}/cancel"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the 202, not the 409
    assert rows[0].actor == "admin-pc6@example.com" and rows[0].entity_id == run.run_id


async def test_an_unavailable_cancel_channel_is_a_503_and_leaves_the_run_going(
    client, make_user, in_flight, monkeypatch: pytest.MonkeyPatch
):
    from src.api.routes import agents as agents_routes

    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-pc7@example.com")
    run = in_flight(headers)
    await run.start()

    async def _redis_down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(agents_routes, "request_cancel", _redis_down)
    refused = await client.post(
        f"/api/v1/agents/pipeline/runs/{run.run_id}/cancel", headers=headers
    )

    assert refused.status_code == 503 and "Redis" in refused.json()["detail"]
    assert (await run.finish()).json()["cancelled"] is False  # nothing was cancelled
