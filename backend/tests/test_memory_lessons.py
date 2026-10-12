"""What agent memory may learn from (src/memory/lessons.py), and how the two
pipelines use it.

Memory is replayed into later LLM prompts as "lessons from earlier similar
runs", so a false lesson is misinformation with authority. The first version of
this feature recorded the LangGraph pipeline's stand-in results -- a Sharpe of
1.0 nobody computed, a "deployment" that deploys nothing -- as if they were
outcomes. These tests pin the rule that replaced it: a lesson is recorded only
if the system can vouch for the fact behind it. Today that means generated code
that failed static validation or its sandbox smoke run, and never anything
derived from a backtest, a paper fill or a stand-in node.

The only fakes are the edges a test cannot reach for real: the LLM (`_Router`),
the Redis cancel flag (`_Signal`), and -- where a test needs a sandbox outcome
it cannot make real -- the sandbox runtime (`_Sandbox`). The HTTP tests run the
real routes, `PipelineMemory`, `MemoryService` and an in-process Qdrant.
"""

import asyncio
import hashlib
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from qdrant_client import AsyncQdrantClient

from src.agents import graph as graph_module
from src.agents.graph import MAX_VALIDATION_ATTEMPTS, PipelineCancelledError, run_pipeline
from src.agents.state import TradingOSGraphState
from src.core.config import Settings
from src.core.roles import Role
from src.engine.options_grounding import NakedOptionsScanResult
from src.engine.sandbox.process_runtime import (
    RestrictedProcessSandboxRuntime,
    SandboxWorkerCrashedError,
)
from src.engine.sandbox.types import SandboxLimits, SandboxResult
from src.main import app
from src.memory import lessons as lessons_module
from src.memory.lessons import (
    MAX_QUOTED_CHARS,
    format_lessons_block,
    is_simulated,
    quote,
    quote_errors,
    recall_lessons,
    record_lesson,
)
from src.memory.service import MemoryService, get_memory_service
from src.memory.store import QdrantMemoryStore
from src.models.strategy import InstrumentClass
from src.models.strategy_version import StrategyVersion
from src.orchestration import strategies as strategies_module
from src.orchestration.strategies import (
    create_strategy,
    create_version_with_validation,
    generate_strategy_code,
    remember_code_failure,
    run_strategy_pipeline,
)
from src.orchestration.strategy_suggestions import (
    regenerate_from_suggestion,
    review_suggestion,
    submit_suggestion,
)

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
_BANNED_IMPORT_CODE = "import os\n"


# ---- fakes ---------------------------------------------------------------------


class _Memory:
    """An `AgentMemory` that records what it is asked."""

    def __init__(self, lessons: list[str] | None = None) -> None:
        self.lessons = lessons or []
        self.recalled: list[str] = []
        self.remembered: list[tuple[str, dict[str, Any]]] = []

    async def recall(self, objective: str) -> list[str]:
        self.recalled.append(objective)
        return self.lessons

    async def remember(self, text: str, metadata: dict[str, Any]) -> None:
        self.remembered.append((text, metadata))


class _ExplodingMemory:
    async def recall(self, objective: str) -> list[str]:
        raise RuntimeError("recall exploded")

    async def remember(self, text: str, metadata: dict[str, Any]) -> None:
        raise RuntimeError("remember exploded")


class _Signal:
    """Reports 'cancelled' once more than `cancel_after` boundaries were checked."""

    def __init__(self, cancel_after: int) -> None:
        self.cancel_after = cancel_after
        self.checks = 0

    async def is_cancelled(self) -> bool:
        self.checks += 1
        return self.checks > self.cancel_after


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

    def payload_of(self, event_type: str, node: str | None = None) -> dict[str, Any]:
        (payload,) = (
            p for t, n, p in self.events if t == event_type and (node is None or n == node)
        )
        assert payload is not None
        return payload


class _Router:
    """An `LlmRouter` that answers every prompt with `code`, keeping the prompts."""

    def __init__(self, code: str = _VALID_CODE) -> None:
        self.code = code
        self.prompts: list[str] = []

    async def complete(self, *, agent_id: str, prompt: str) -> Any:
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.code, provider=SimpleNamespace(value="test"))


class _Sandbox:
    """A `SandboxRuntime` with a canned outcome: a result, or an infrastructure fault."""

    def __init__(
        self, result: SandboxResult | None = None, raises: Exception | None = None
    ) -> None:
        self.result = result or SandboxResult(success=True, result={"trades": 0})
        self.raises = raises
        self.calls = 0

    def execute(
        self,
        code: str,
        *,
        params: dict[str, Any],
        scratch_dir: Path,
        data_dir: Path,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class _WordEmbedder:
    """Hashes words into a fixed-size vector so texts sharing words are near each other."""

    name = "test-words"
    model = "bag-of-words"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * 64
            for word in text.lower().split():
                digest = hashlib.sha256(word.strip(".,:;'\"()").encode()).digest()
                vec[int.from_bytes(digest[:4], "big") % 64] += 1.0
            out.append(vec if any(vec) else [1.0] + [0.0] * 63)
        return out


# ---- the helpers ---------------------------------------------------------------


def test_is_simulated_reads_only_an_explicit_true_marker():
    assert is_simulated(None) is True  # nothing was produced, so there is nothing to learn from
    assert is_simulated({"simulated": True, "sharpe": 1.0}) is True
    assert is_simulated({"sharpe": 1.0}) is False
    assert is_simulated({}) is False
    assert is_simulated({"simulated": False}) is False
    assert is_simulated({"simulated": "true"}) is False  # not truthy-ish: a literal True only
    assert is_simulated("simulated") is False


def test_quote_flattens_to_one_bounded_line():
    assert quote("short") == "short"
    assert quote("a\n  b\t c") == "a b c"
    long = quote("x" * 500)
    assert len(long) == MAX_QUOTED_CHARS and long.endswith("...")
    assert quote("y" * 20, limit=10) == "y" * 7 + "..."


def test_quote_errors_shows_the_first_few_and_counts_the_rest():
    assert quote_errors(None) == "" and quote_errors([]) == ""
    assert quote_errors(["a", "b"]) == "a; b"
    assert quote_errors([f"error {i}" for i in range(5)]) == "error 0; error 1; error 2 (+2 more)"
    assert quote_errors(["z" * 400]) == "z" * 157 + "..."


def test_the_lessons_block_is_empty_without_lessons_and_a_bullet_list_otherwise():
    assert format_lessons_block([]) == ""
    assert format_lessons_block(["one", "two"]) == (
        "\n\nLessons from earlier similar runs (use if relevant):\n- one\n- two"
    )


async def test_recall_and_record_never_raise_and_tolerate_no_memory():
    assert await recall_lessons(None, "obj") == []
    assert await recall_lessons(_ExplodingMemory(), "obj") == []
    await record_lesson(None, "text", {})
    await record_lesson(_ExplodingMemory(), "text", {})  # must not raise

    memory = _Memory(["a lesson"])
    assert await recall_lessons(memory, "obj") == ["a lesson"] and memory.recalled == ["obj"]
    await record_lesson(memory, "text", {"k": 1})
    assert memory.remembered == [("text", {"k": 1})]


async def test_a_hung_memory_costs_a_run_a_bounded_delay_not_a_stall(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Hung:
        async def recall(self, objective: str) -> list[str]:
            await asyncio.sleep(5)
            return ["too late"]

        async def remember(self, text: str, metadata: dict[str, Any]) -> None:
            await asyncio.sleep(5)

    monkeypatch.setattr(lessons_module, "MEMORY_CALL_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()

    assert await recall_lessons(_Hung(), "obj") == []
    await record_lesson(_Hung(), "text", {})

    assert time.monotonic() - started < 2  # two bounded waits, not two five-second ones


# ---- the LangGraph pipeline: stand-ins say so ------------------------------------

_STAND_IN_FIELDS = (
    "market_analysis",
    "options_legs",
    "compliance_verdict",
    "backtest_metrics",
    "evaluation_verdict",
    "optimization_result",
    "risk_assessment",
    "deployment_result",
)


async def test_every_stand_in_node_marks_its_output_simulated():
    state = await run_pipeline("Build a momentum strategy")

    for name in _STAND_IN_FIELDS:
        value = getattr(state, name)
        assert value is not None, name
        assert is_simulated(value), name
        # First key, so a feed summary that shows only a few fields still shows it.
        assert next(iter(value)) == "simulated", name

    # The nodes that do real work never claim to be stand-ins.
    assert state.validation_result is not None and "simulated" not in state.validation_result
    assert "simulated" not in (state.ceo_brief or {})
    assert "simulated" not in (state.strategy or {})


async def test_an_evaluation_is_simulated_exactly_when_its_inputs_are():
    evaluate = graph_module.NODE_FUNCTIONS["evaluation"]
    router: Any = None  # the evaluation rule never calls an LLM

    placeholder = await evaluate(
        TradingOSGraphState(objective="o", backtest_metrics={"simulated": True, "sharpe": 1.0}),
        router,
    )
    assert placeholder["evaluation_verdict"]["simulated"] is True

    missing = await evaluate(TradingOSGraphState(objective="o"), router)
    assert missing["evaluation_verdict"] == {"simulated": True, "verdict": "fail", "sharpe": 0.0}

    measured = await evaluate(
        TradingOSGraphState(objective="o", backtest_metrics={"sharpe": 0.2}), router
    )
    assert measured["evaluation_verdict"] == {"simulated": False, "verdict": "fail", "sharpe": 0.2}


async def test_the_feed_says_a_step_was_simulated_and_the_run_does_not_claim_a_deployment():
    sink = _Sink()

    state = await run_pipeline("obj", event_sink=sink)

    assert state.deployment_result == {"simulated": True, "deployed": True, "target": "paper"}
    output = sink.payload_of("step.completed", "backtesting")["output"]
    assert next(iter(output["backtest_metrics"])) == "simulated"
    assert output["backtest_metrics"]["simulated"] is True
    completed = sink.payload_of("pipeline.completed")
    assert completed["simulated"] is True
    assert completed["deployed"] is False  # the stand-in deploys nothing
    assert completed["evaluation_verdict"] == "pass"  # still reported, next to the marker


# ---- the LangGraph pipeline: what it records ---------------------------------------


async def _bad_code(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
    return {"generated_code": "def broken(:", "node_log": [*state.node_log, "code_generation"]}


async def test_a_normal_run_recalls_but_records_nothing():
    memory = _Memory(["an earlier lesson"])

    state = await run_pipeline("Build a momentum strategy", run_id="run-1", memory=memory)

    assert memory.recalled == ["Build a momentum strategy"]
    assert state.memory_context == ["an earlier lesson"]
    # Every result in this run -- backtest, evaluation, deployment -- is a stand-in's.
    assert memory.remembered == []


async def test_no_performance_lesson_is_recorded_even_when_the_result_is_not_marked_simulated(
    monkeypatch: pytest.MonkeyPatch,
):
    """A real backtest would not carry the marker -- but nothing records whether
    its data was real, so even that is not remembered. This pins the rule so that
    adding provenance later is a deliberate change, not a drift."""
    calls = {"n": 0}

    async def _backtest(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "backtest_metrics": {"sharpe": 0.0 if calls["n"] <= 2 else 1.0},
            "node_log": [*state.node_log, "backtesting"],
        }

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "backtesting", _backtest)
    memory = _Memory()

    state = await run_pipeline("obj", memory=memory)

    assert state.rejection_count == 2 and state.deployment_result is not None
    assert memory.remembered == []


@pytest.mark.parametrize(
    ("code", "expected_error"),
    [("def broken(:", "SyntaxError"), (_BANNED_IMPORT_CODE, "banned import: os")],
)
async def test_code_that_never_passes_validation_is_remembered_with_why(
    monkeypatch: pytest.MonkeyPatch, code: str, expected_error: str
):
    async def _node(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        return {"generated_code": code, "node_log": [*state.node_log, "code_generation"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "code_generation", _node)
    memory = _Memory()

    state = await run_pipeline("Momentum on NIFTY\nwith  spacing", run_id="run-9", memory=memory)

    assert state.evaluation_verdict is None
    assert state.validation_attempts == MAX_VALIDATION_ATTEMPTS
    ((text, metadata),) = memory.remembered
    assert text.startswith(
        "Objective 'Momentum on NIFTY with spacing': the generated code failed validation "
        f"after {MAX_VALIDATION_ATTEMPTS} attempt(s): "
    )
    assert expected_error in text
    assert metadata == {"event": "validation_failed", "source": "pipeline", "run_id": "run-9"}


async def test_code_that_passes_validation_on_a_retry_is_not_a_lesson(
    monkeypatch: pytest.MonkeyPatch,
):
    attempts = {"n": 0}

    async def _flaky(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        attempts["n"] += 1
        code = "def broken(:" if attempts["n"] == 1 else _VALID_CODE
        return {"generated_code": code, "node_log": [*state.node_log, "code_generation"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "code_generation", _flaky)
    memory = _Memory()

    state = await run_pipeline("obj", memory=memory)

    assert (state.validation_result or {}).get("valid") is True and state.validation_attempts == 2
    assert memory.remembered == []


async def test_a_cancelled_run_records_nothing_even_when_it_would_have_had_a_lesson(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "code_generation", _bad_code)
    control = _Memory()
    await run_pipeline("obj", memory=control)
    assert len(control.remembered) == 1  # the same run, left alone, does leave a lesson

    memory = _Memory()
    with pytest.raises(PipelineCancelledError):
        await run_pipeline("obj", memory=memory, cancel_signal=_Signal(cancel_after=8))

    assert memory.remembered == []


async def test_a_raising_memory_cannot_break_a_run_that_has_a_lesson_to_record(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "code_generation", _bad_code)

    state = await run_pipeline("obj", memory=_ExplodingMemory())

    assert state.validation_attempts == MAX_VALIDATION_ATTEMPTS and state.memory_context == []


# ---- the strategy pipeline: what it records ------------------------------------------


def _version(**fields: Any) -> StrategyVersion:
    """A version as the pipeline leaves it -- built in memory, no database needed."""
    base: dict[str, Any] = {
        "strategy_id": uuid.uuid4(),
        "version_number": 1,
        "code": _VALID_CODE,
        "static_validation_passed": True,
        "static_validation_errors": None,
        "sandbox_result": None,
    }
    return StrategyVersion(**{**base, **fields})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"static_validation_passed": False, "static_validation_errors": ["banned import: os"]},
            ("static_validation_failed", "failed static validation: banned import: os."),
            id="static validation failed",
        ),
        pytest.param(
            {"static_validation_passed": False},
            ("static_validation_failed", "failed static validation: no detail."),
            id="static validation failed without detail",
        ),
        pytest.param(
            {"sandbox_result": {"success": False, "error": "ValueError: boom"}},
            (
                "sandbox_failed",
                "failed its sandbox smoke run (called with empty data): ValueError: boom.",
            ),
            id="sandbox run failed",
        ),
        pytest.param(
            {"sandbox_result": {"success": False, "error": None}},
            ("sandbox_failed", "(called with empty data): no detail."),
            id="sandbox run failed without detail",
        ),
        pytest.param({}, None, id="valid but never run"),
        pytest.param({"sandbox_result": {"success": True, "result": {}}}, None, id="all passed"),
        pytest.param(
            # The naked-options scan clears `sandbox_passed` but not the code's own run.
            {"sandbox_passed": False, "sandbox_result": {"success": True, "result": {}}},
            None,
            id="blocked by the naked-options scan",
        ),
    ],
)
async def test_remember_code_failure_records_only_failures_of_the_code_itself(
    fields: dict[str, Any], expected: tuple[str, str] | None
):
    memory = _Memory()

    await remember_code_failure(memory, "Momentum on NIFTY", _version(**fields))

    if expected is None:
        assert memory.remembered == []
        return
    event, fragment = expected
    ((text, metadata),) = memory.remembered
    assert text.startswith("Objective 'Momentum on NIFTY': the generated code ")
    assert fragment in text
    assert metadata["event"] == event and metadata["source"] == "strategy_pipeline"
    assert metadata["version"] == 1 and uuid.UUID(metadata["strategy_id"])


async def test_a_lesson_is_a_pointer_not_a_log():
    version = _version(static_validation_passed=False, static_validation_errors=["e" * 1000] * 10)
    memory = _Memory()

    await remember_code_failure(memory, "o" * 1000, version)

    ((text, _),) = memory.remembered
    assert len(text) < 1000 and "(+7 more)" in text


async def test_without_memory_a_code_failure_is_simply_not_recorded():
    await remember_code_failure(None, "obj", _version(static_validation_passed=False))


async def test_a_passing_strategy_records_no_lesson(db_session_factory):
    memory = _Memory()
    sandbox = _Sandbox()

    async with db_session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Momentum on NIFTY",
            router=_Router(),
            sandbox_runtime=sandbox,
            memory=memory,
        )

    assert version.sandbox_passed is True and sandbox.calls == 1
    assert memory.recalled == ["Momentum on NIFTY"] and memory.remembered == []


async def test_code_that_fails_static_validation_is_remembered_and_never_reaches_the_sandbox(
    db_session_factory,
):
    memory = _Memory()
    sandbox = _Sandbox()

    async with db_session_factory() as db:
        strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Momentum on NIFTY",
            router=_Router(_BANNED_IMPORT_CODE),
            sandbox_runtime=sandbox,
            memory=memory,
        )

    assert version.static_validation_passed is False and sandbox.calls == 0
    ((text, metadata),) = memory.remembered
    assert text == (
        "Objective 'Momentum on NIFTY': the generated code failed static validation: "
        "banned import: os."
    )
    assert metadata == {
        "event": "static_validation_failed",
        "source": "strategy_pipeline",
        "strategy_id": str(strategy.id),
        "version": 1,
    }


async def test_code_that_fails_in_the_sandbox_is_remembered(db_session_factory):
    memory = _Memory()
    sandbox = _Sandbox(SandboxResult(success=False, error="ZeroDivisionError: division by zero"))

    async with db_session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Momentum on NIFTY",
            router=_Router(),
            sandbox_runtime=sandbox,
            memory=memory,
        )

    assert version.sandbox_passed is False
    ((text, metadata),) = memory.remembered
    assert text == (
        "Objective 'Momentum on NIFTY': the generated code failed its sandbox smoke run "
        "(called with empty data): ZeroDivisionError: division by zero."
    )
    assert metadata["event"] == "sandbox_failed"


async def test_the_real_sandboxs_error_is_what_gets_remembered(db_session_factory):
    memory = _Memory()
    code = 'def run_backtest(data, config):\n    raise ValueError("boom")\n'

    async with db_session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Momentum on NIFTY",
            router=_Router(code),
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
            memory=memory,
        )

    assert version.static_validation_passed is True and version.sandbox_passed is False
    ((text, _),) = memory.remembered
    assert text.endswith("(called with empty data): ValueError: boom.")


async def test_a_naked_options_block_is_not_a_lesson(
    db_session_factory, monkeypatch: pytest.MonkeyPatch
):
    """The legs are fixed by `_default_option_legs` (and grounding depends on
    chain data being available), not written by the model -- so a block says
    nothing the code generator could act on."""
    monkeypatch.setattr(
        strategies_module,
        "naked_options_scan",
        lambda legs: NakedOptionsScanResult(blocked=True, reasons=["naked short call"]),
    )
    memory = _Memory()

    async with db_session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Iron condor on NIFTY",
            instrument_class=InstrumentClass.OPTIONS.value,
            router=_Router(),
            sandbox_runtime=_Sandbox(),
            memory=memory,
        )

    assert version.sandbox_passed is False  # the scan blocked it...
    assert (version.sandbox_result or {}).get("success") is True  # ...the code itself was fine
    assert memory.remembered == []


async def test_an_infrastructure_fault_in_the_sandbox_is_not_a_lesson(db_session_factory):
    memory = _Memory()

    async with db_session_factory() as db:
        with pytest.raises(SandboxWorkerCrashedError):
            await run_strategy_pipeline(
                db,
                name="S",
                objective="Momentum on NIFTY",
                router=_Router(),
                sandbox_runtime=_Sandbox(raises=SandboxWorkerCrashedError("worker died")),
                memory=memory,
            )

    assert memory.remembered == []


async def test_a_raising_memory_cannot_break_the_strategy_pipeline(db_session_factory):
    async with db_session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name="S",
            objective="Momentum on NIFTY",
            router=_Router(_BANNED_IMPORT_CODE),  # a failure to record, and a recall to survive
            sandbox_runtime=_Sandbox(),
            memory=_ExplodingMemory(),
        )

    assert version.static_validation_passed is False


async def test_recalled_lessons_reach_the_code_generation_prompt_after_the_task():
    router = _Router()
    lessons = ["Objective 'x': the generated code failed static validation: banned import: os."]

    await generate_strategy_code("Momentum on NIFTY", router=router, memory=_Memory(lessons))

    (prompt,) = router.prompts
    task = "Write a run_backtest(data, config) -> dict implementation for: Momentum on NIFTY"
    assert prompt == task + format_lessons_block(lessons)


@pytest.mark.parametrize("memory", [None, _Memory(), _ExplodingMemory()])
async def test_without_lessons_the_code_generation_prompt_is_exactly_what_it_was(memory: Any):
    router = _Router()

    await generate_strategy_code("Momentum on NIFTY", router=router, memory=memory)

    assert router.prompts == [
        "Write a run_backtest(data, config) -> dict implementation for: Momentum on NIFTY"
    ]


async def test_regenerated_code_that_fails_validation_is_remembered(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="Momentum for NIFTY")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
        )
    async with db_session_factory() as db:
        await review_suggestion(db, suggestion.id)
    memory = _Memory(["an earlier lesson"])
    router = _Router(_BANNED_IMPORT_CODE)

    async with db_session_factory() as db:
        new_version = await regenerate_from_suggestion(
            db, suggestion.id, router=router, memory=memory
        )

    assert new_version is not None and new_version.static_validation_passed is False
    objective = "Momentum for NIFTY\n\nIncorporate this suggested change: Add a stop-loss"
    assert memory.recalled == [objective]
    assert router.prompts[0].endswith(format_lessons_block(["an earlier lesson"]))
    ((text, metadata),) = memory.remembered
    assert text == (
        "Objective 'Momentum for NIFTY Incorporate this suggested change: Add a stop-loss': "
        "the generated code failed static validation: banned import: os."
    )
    assert metadata["event"] == "static_validation_failed" and metadata["version"] == 2


# ---- end to end through the HTTP route, PipelineMemory, MemoryService and Qdrant ----


@pytest.fixture
def memory_service():
    service = MemoryService(
        Settings(_env_file=None),
        store=QdrantMemoryStore(AsyncQdrantClient(":memory:")),
        embedder=_WordEmbedder(),
    )
    app.dependency_overrides[get_memory_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_memory_service, None)


async def _admin_headers(client: AsyncClient, make_user: Any) -> dict[str, str]:
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_a_failed_strategy_teaches_the_next_request_through_the_real_stack(
    client: AsyncClient,
    make_user: Any,
    memory_service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _admin_headers(client, make_user)
    router = _Router(_BANNED_IMPORT_CODE)
    monkeypatch.setattr(strategies_module, "get_llm_router", lambda: router)
    monkeypatch.setattr(strategies_module, "get_default_sandbox_runtime", lambda: _Sandbox())
    objective = "Momentum breakout strategy for NIFTY futures"

    # 1. The model writes code with a banned import; the strategy is created, flagged invalid...
    first = await client.post(
        "/api/v1/strategies", json={"name": "S1", "objective": objective}, headers=headers
    )
    assert first.status_code == 201
    assert first.json()["versions"][0]["static_validation_passed"] is False

    # ...and exactly one lesson about it is in memory.
    ((_, lesson),) = await memory_service.query(objective, kind="strategy", limit=10)
    assert "failed static validation: banned import: os" in lesson.text
    assert lesson.payload["source"] == "strategy_pipeline"
    assert lesson.payload["event"] == "static_validation_failed"

    # 2. A similar request is told about it, in the code-generation prompt itself.
    router.code = _VALID_CODE
    second = await client.post(
        "/api/v1/strategies",
        json={"name": "S2", "objective": "Momentum breakout for NIFTY"},
        headers=headers,
    )
    assert second.status_code == 201
    assert second.json()["versions"][0]["static_validation_passed"] is True
    assert lesson.text in router.prompts[-1]

    # 3. Failing the same way again is the same memory, not a second copy of it.
    router.code = _BANNED_IMPORT_CODE
    third = await client.post(
        "/api/v1/strategies", json={"name": "S3", "objective": objective}, headers=headers
    )
    assert third.status_code == 201
    assert len(await memory_service.query(objective, kind="strategy", limit=10)) == 1


async def test_regenerating_from_a_suggestion_remembers_a_failure_through_the_route(
    client: AsyncClient,
    make_user: Any,
    memory_service: MemoryService,
    db_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _admin_headers(client, make_user)
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="Momentum for NIFTY")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
        )
    async with db_session_factory() as db:
        await review_suggestion(db, suggestion.id)
    monkeypatch.setattr(strategies_module, "get_llm_router", lambda: _Router(_BANNED_IMPORT_CODE))

    response = await client.post(
        f"/api/v1/strategies/{strategy.id}/suggestions/{suggestion.id}/regenerate",
        headers=headers,
    )

    assert response.status_code == 200
    ((_, lesson),) = await memory_service.query("Momentum for NIFTY", kind="strategy", limit=10)
    assert "failed static validation: banned import: os" in lesson.text
    assert lesson.payload["event"] == "static_validation_failed"


async def test_a_normal_pipeline_run_leaves_memory_exactly_as_it_found_it(
    client: AsyncClient, make_user: Any, memory_service: MemoryService
):
    """The original bug, through the real route and a real vector store: running
    the LangGraph pipeline used to write the stand-in results of that run into
    memory. It recalls what is there (a human note) and writes nothing."""
    headers = await _admin_headers(client, make_user)
    note = "momentum failed in sideways markets"
    await memory_service.remember("strategy", note, {"source": "operator"})

    response = await client.post(
        "/api/v1/agents/pipeline/run",
        json={"objective": "Build a momentum strategy"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["deployment_result"]["simulated"] is True
    hits = await memory_service.query("momentum", kind="strategy", limit=10)
    assert [hit.text for _, hit in hits] == [note]
