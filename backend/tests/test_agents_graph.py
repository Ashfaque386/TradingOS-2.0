"""LangGraph pipeline tests (Build Spec §7.2): the disabled-agent circuit
breaker (requirement 3's test) plus enough coverage of the routing logic
(validator retry cap, evaluation fail -> memory ingest -> CEO escalation)
to trust the 13-node wiring end to end.
"""

from src.agents import graph as graph_module
from src.agents.graph import run_pipeline
from src.gateway.roster import ROSTER_IDS


async def test_disabled_agent_node_logic_is_never_invoked(monkeypatch):
    called = False

    async def _spy(state, router):
        nonlocal called
        called = True
        return {"market_analysis": {"trend": "should never be set"}}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _spy)

    enabled = frozenset(ROSTER_IDS - {"market-analyst"})
    state = await run_pipeline("obj", enabled_agents=enabled)

    assert called is False, "a disabled agent's real node function must never be called"
    assert state.market_analysis is None
    assert any(
        entry == "market_analysis_step:skipped(agent-disabled:market-analyst)"
        for entry in state.node_log
    )


async def test_enabled_agent_runs_its_real_node_logic(monkeypatch):
    called = False

    async def _spy(state, router):
        nonlocal called
        called = True
        return {"market_analysis": {"trend": "ran"}, "node_log": [*state.node_log, "x"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "market_analysis_step", _spy)

    state = await run_pipeline("obj")  # enabled_agents=None -> everyone enabled

    assert called is True
    assert state.market_analysis == {"trend": "ran"}


async def test_full_happy_path_reaches_deployment():
    state = await run_pipeline("Build a momentum strategy for NIFTY")

    assert state.node_log[0] == "ceo_kickoff"
    assert state.node_log[-1] == "deployment"
    assert state.deployment_result == {"deployed": True, "target": "paper"}
    assert state.rejection_count == 0


async def test_validation_failure_loop_ends_after_max_attempts(monkeypatch):
    async def _bad_code_gen(state, router):
        return {"generated_code": "import os\n", "node_log": [*state.node_log, "code_generation"]}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "code_generation", _bad_code_gen)

    state = await run_pipeline("obj")

    assert state.validation_attempts == 3
    assert state.deployment_result is None
    assert state.node_log.count("code_validation") == 3


async def test_evaluation_failure_escalates_to_ceo_after_max_rejections(monkeypatch):
    calls = {"n": 0}

    async def _backtest_then_recover(state, router):
        calls["n"] += 1
        sharpe = 0.0 if calls["n"] <= 5 else 1.0
        return {
            "backtest_metrics": {"sharpe": sharpe},
            "node_log": [*state.node_log, "backtesting"],
        }

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "backtesting", _backtest_then_recover)

    state = await run_pipeline("obj")

    assert state.rejection_count == 5
    assert state.node_log.count("memory_ingest") == 5
    assert state.node_log.count("ceo_kickoff") == 2  # initial kickoff + the escalation re-entry
    assert state.deployment_result is not None  # eventually recovers and completes
