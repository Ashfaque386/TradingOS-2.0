"""LangGraph pipeline (Build Spec §7.2): strategy research -> deployment,
13 nodes, each owned by one fixed-roster agent (src.agents.roster.
PIPELINE_NODE_AGENTS).

Circuit breaker (requirement 3): build_graph() decides, once, at graph-build
time, whether each node gets its real logic function or a skip stand-in --
a disabled agent's real node function is never even looked up, let alone
called, for the lifetime of the compiled graph. This is enforced at
build-time, not by an if-check inside the node itself, so there's no code
path in a disabled node's turn that could reach the real logic.

LLM-backed nodes (CEO, Strategy Generator, Options Strategy Agent, Python
Code Generator) go through the LLM router (src.agents.llm_router); every
other node is deterministic, same "stub capability" honesty as Phase 2's
orchestration/capabilities.py. This sandbox has no LLM provider keys and
blocked egress to provider APIs, so a real call always exhausts the
fallback chain -- _llm_or_fallback() catches that and falls back to a
deterministic canned response rather than crashing the pipeline, logging
that it did so. Against a configured provider (a real deployment, or a
cheap/small dev model wired up locally via Ollama), the same code path
serves the real completion instead.
"""

from collections.abc import Awaitable, Callable

import structlog
from langgraph.graph import END, StateGraph

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.agents.roster import PIPELINE_NODE_AGENTS
from src.agents.state import TradingOSGraphState
from src.agents.tools.code_lint import code_format_lint
from src.gateway.roster import ROSTER_IDS

logger = structlog.get_logger(__name__)

MAX_VALIDATION_ATTEMPTS = 3
MAX_REJECTIONS = 5

NodeFn = Callable[[TradingOSGraphState, LlmRouter], Awaitable[dict]]


async def _llm_or_fallback(router: LlmRouter, agent_id: str, prompt: str, fallback: dict) -> dict:
    try:
        result = await router.complete(agent_id=agent_id, prompt=prompt)
        return {"source": "llm", "provider": result.provider.value, "text": result.text}
    except LlmRouterExhaustedError:
        logger.warning("agents.graph.llm_unavailable_using_fallback", agent_id=agent_id)
        return {"source": "fallback", **fallback}


async def _node_ceo_kickoff(state: TradingOSGraphState, router: LlmRouter) -> dict:
    brief = await _llm_or_fallback(
        router,
        "ceo-agent",
        f"Kick off a trading strategy research objective: {state.objective}",
        fallback={"directive": f"Research and propose a strategy for: {state.objective}"},
    )
    return {"ceo_brief": brief, "node_log": [*state.node_log, "ceo_kickoff"]}


async def _node_market_analysis(state: TradingOSGraphState, router: LlmRouter) -> dict:
    analysis = {"trend": "neutral", "confidence": 0.5, "objective": state.objective}
    return {"market_analysis": analysis, "node_log": [*state.node_log, "market_analysis"]}


async def _node_strategy_generation(state: TradingOSGraphState, router: LlmRouter) -> dict:
    strategy = await _llm_or_fallback(
        router,
        "strategy-generator",
        f"Generate a trading strategy given market analysis: {state.market_analysis}",
        fallback={"name": "fallback-momentum-strategy", "kind": "momentum"},
    )
    return {"strategy": strategy, "node_log": [*state.node_log, "strategy_generation"]}


async def _node_options_strategy(state: TradingOSGraphState, router: LlmRouter) -> dict:
    legs = {"legs": [], "naked_options_found": False}
    return {"options_legs": legs, "node_log": [*state.node_log, "options_strategy"]}


async def _node_code_generation(state: TradingOSGraphState, router: LlmRouter) -> dict:
    result = await _llm_or_fallback(
        router,
        "python-code-generator",
        f"Write a run_backtest(data, config) implementation for strategy: {state.strategy}",
        fallback={
            "text": "def run_backtest(data, config):\n    return {'trades': 0}\n",
        },
    )
    return {
        "generated_code": result.get("text", ""),
        "node_log": [*state.node_log, "code_generation"],
    }


async def _node_compliance_check(state: TradingOSGraphState, router: LlmRouter) -> dict:
    verdict = {"blocked": False, "reason": None}
    return {"compliance_verdict": verdict, "node_log": [*state.node_log, "compliance_check"]}


async def _node_code_validation(state: TradingOSGraphState, router: LlmRouter) -> dict:
    result = code_format_lint({"code": state.generated_code or ""})
    return {
        "validation_result": result,
        "validation_attempts": state.validation_attempts + 1,
        "node_log": [*state.node_log, "code_validation"],
    }


async def _node_backtesting(state: TradingOSGraphState, router: LlmRouter) -> dict:
    metrics = {"sharpe": 1.0, "max_drawdown": 0.1}
    return {"backtest_metrics": metrics, "node_log": [*state.node_log, "backtesting"]}


async def _node_evaluation(state: TradingOSGraphState, router: LlmRouter) -> dict:
    sharpe = (state.backtest_metrics or {}).get("sharpe", 0.0)
    verdict = {"verdict": "pass" if sharpe >= 0.5 else "fail", "sharpe": sharpe}
    return {"evaluation_verdict": verdict, "node_log": [*state.node_log, "evaluation"]}


async def _node_optimization(state: TradingOSGraphState, router: LlmRouter) -> dict:
    result = {"improved": True}
    return {"optimization_result": result, "node_log": [*state.node_log, "optimization"]}


async def _node_risk_assessment(state: TradingOSGraphState, router: LlmRouter) -> dict:
    result = {"within_limits": True}
    return {"risk_assessment": result, "node_log": [*state.node_log, "risk_assessment"]}


async def _node_deployment(state: TradingOSGraphState, router: LlmRouter) -> dict:
    result = {"deployed": True, "target": "paper"}
    return {"deployment_result": result, "node_log": [*state.node_log, "deployment"]}


async def _node_memory_ingest(state: TradingOSGraphState, router: LlmRouter) -> dict:
    return {
        "rejection_count": state.rejection_count + 1,
        "node_log": [*state.node_log, "memory_ingest"],
    }


NODE_FUNCTIONS: dict[str, NodeFn] = {
    "ceo_kickoff": _node_ceo_kickoff,
    "market_analysis_step": _node_market_analysis,
    "strategy_generation": _node_strategy_generation,
    "options_strategy": _node_options_strategy,
    "code_generation": _node_code_generation,
    "compliance_check": _node_compliance_check,
    "code_validation": _node_code_validation,
    "backtesting": _node_backtesting,
    "evaluation": _node_evaluation,
    "optimization": _node_optimization,
    "risk_assessment_step": _node_risk_assessment,
    "deployment": _node_deployment,
    "memory_ingest": _node_memory_ingest,
}

assert set(NODE_FUNCTIONS) == {name for name, _ in PIPELINE_NODE_AGENTS}


def _route_after_compliance(state: TradingOSGraphState) -> str:
    if (state.compliance_verdict or {}).get("blocked"):
        return "end"
    return "validator"


def _route_after_validation(state: TradingOSGraphState) -> str:
    if (state.validation_result or {}).get("valid"):
        return "backtesting"
    if state.validation_attempts >= MAX_VALIDATION_ATTEMPTS:
        return "end"
    return "retry"


def _route_after_evaluation(state: TradingOSGraphState) -> str:
    verdict = (state.evaluation_verdict or {}).get("verdict")
    return "optimization" if verdict == "pass" else "memory_ingest"


def _route_after_memory_ingest(state: TradingOSGraphState) -> str:
    return "ceo" if state.rejection_count >= MAX_REJECTIONS else "strategy"


def _make_skip_node(
    node_name: str, agent_id: str
) -> Callable[[TradingOSGraphState], Awaitable[dict]]:
    async def node(state: TradingOSGraphState) -> dict:
        return {"node_log": [*state.node_log, f"{node_name}:skipped(agent-disabled:{agent_id})"]}

    return node


def build_graph(enabled_agents: frozenset[str] | None = None, *, router: LlmRouter | None = None):
    """Compiles the 13-node pipeline. enabled_agents defaults to "every
    roster agent enabled"; pass the subset that's actually enabled (e.g.
    from src.gateway.state's live effective-agent config) to have every
    other agent's node wired to the skip stand-in instead of its real
    logic -- decided once, here, not re-checked per node invocation.
    """
    if enabled_agents is None:
        enabled_agents = ROSTER_IDS
    bound_router = router if router is not None else get_llm_router()

    graph = StateGraph(TradingOSGraphState)

    for node_name, agent_id in PIPELINE_NODE_AGENTS:
        if agent_id in enabled_agents:
            real_fn = NODE_FUNCTIONS[node_name]

            async def node(state: TradingOSGraphState, _fn: NodeFn = real_fn) -> dict:
                return await _fn(state, bound_router)
        else:
            node = _make_skip_node(node_name, agent_id)
        graph.add_node(node_name, node)

    graph.set_entry_point("ceo_kickoff")
    graph.add_edge("ceo_kickoff", "market_analysis_step")
    graph.add_edge("market_analysis_step", "strategy_generation")
    graph.add_edge("strategy_generation", "options_strategy")
    graph.add_edge("options_strategy", "code_generation")
    graph.add_edge("code_generation", "compliance_check")
    graph.add_conditional_edges(
        "compliance_check", _route_after_compliance, {"validator": "code_validation", "end": END}
    )
    graph.add_conditional_edges(
        "code_validation",
        _route_after_validation,
        {"backtesting": "backtesting", "retry": "code_generation", "end": END},
    )
    graph.add_edge("backtesting", "evaluation")
    graph.add_conditional_edges(
        "evaluation",
        _route_after_evaluation,
        {"optimization": "optimization", "memory_ingest": "memory_ingest"},
    )
    graph.add_edge("optimization", "risk_assessment_step")
    graph.add_edge("risk_assessment_step", "deployment")
    graph.add_edge("deployment", END)
    graph.add_conditional_edges(
        "memory_ingest",
        _route_after_memory_ingest,
        {"ceo": "ceo_kickoff", "strategy": "strategy_generation"},
    )

    return graph.compile()


async def run_pipeline(
    objective: str,
    *,
    enabled_agents: frozenset[str] | None = None,
    router: LlmRouter | None = None,
    run_id: str | None = None,
) -> TradingOSGraphState:
    compiled = build_graph(enabled_agents, router=router)
    initial = TradingOSGraphState(objective=objective, run_id=run_id)
    # LangGraph's default recursion_limit (25) is well under the step count
    # a full validator-retry (up to MAX_VALIDATION_ATTEMPTS) plus
    # evaluation-rejection (up to MAX_REJECTIONS full strategy-generation
    # loops, each ~8 nodes) run can legitimately take -- raise it rather
    # than let a real retry sequence be mistaken for a runaway graph.
    result = await compiled.ainvoke(initial, config={"recursion_limit": 200})
    return TradingOSGraphState.model_validate(dict(result))
