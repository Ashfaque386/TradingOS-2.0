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

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import structlog
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.agents.roster import PIPELINE_NODE_AGENTS
from src.agents.state import TradingOSGraphState
from src.agents.tools.code_lint import code_format_lint
from src.gateway.roster import ROSTER_IDS
from src.observability.metrics import agent_node_duration_seconds

logger = structlog.get_logger(__name__)

MAX_VALIDATION_ATTEMPTS = 3
MAX_REJECTIONS = 5

NodeFn = Callable[[TradingOSGraphState, LlmRouter], Awaitable[dict[str, Any]]]


class PipelineEventSink(Protocol):
    """Where a pipeline run reports what each step is doing (Phase 19
    Finding #1, the live per-step agent feed). A Protocol, not an import of
    `src.orchestration.events`: this module deliberately stays DB-independent
    (see `run_pipeline`), so whoever owns a database session and Redis hands
    in an implementation -- `src.orchestration.pipeline_events` does."""

    async def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None: ...


async def _safe_emit(
    sink: PipelineEventSink | None,
    event_type: str,
    *,
    node: str | None = None,
    agent_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Observability must never break the pipeline it observes: a sink that
    raises (a dropped DB connection, a full disk) costs a missing feed row
    and a warning, never a failed strategy run."""
    if sink is None:
        return
    try:
        await sink.emit(event_type, node=node, agent_id=agent_id, payload=payload)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("agents.graph.event_sink_failed", event_type=event_type, node=node)


_PREVIEW_CHARS = 300
_MAX_SUMMARY_FIELDS = 12


def _preview(value: str) -> str:
    return value if len(value) <= _PREVIEW_CHARS else value[:_PREVIEW_CHARS] + "..."


def _summarize_output(update: dict[str, Any]) -> dict[str, Any]:
    """A bounded, JSON-safe account of what a node actually produced, for
    the live feed: each state field the node set, reduced to its scalar
    values (a dict's top-level scalars, a long string's length plus a
    preview). Never the whole object -- generated code or an LLM response
    can be large, and the feed needs "what happened", not a data dump."""
    summary: dict[str, Any] = {}
    for key, value in update.items():
        if key == "node_log":
            continue
        if isinstance(value, dict):
            summary[key] = {
                k: (_preview(v) if isinstance(v, str) else v)
                for k, v in list(value.items())[:_MAX_SUMMARY_FIELDS]
                if isinstance(v, str | int | float | bool) or v is None
            }
        elif isinstance(value, str):
            summary[key] = {"chars": len(value), "preview": _preview(value)}
        elif isinstance(value, str | int | float | bool) or value is None:
            summary[key] = value
        else:
            summary[key] = type(value).__name__
    return summary


async def _llm_or_fallback(
    router: LlmRouter, agent_id: str, prompt: str, fallback: dict[str, Any]
) -> dict[str, Any]:
    try:
        result = await router.complete(agent_id=agent_id, prompt=prompt)
        return {"source": "llm", "provider": result.provider.value, "text": result.text}
    except LlmRouterExhaustedError:
        logger.warning("agents.graph.llm_unavailable_using_fallback", agent_id=agent_id)
        return {"source": "fallback", **fallback}


def _with_active_prompt(state: TradingOSGraphState, agent_id: str, task_prompt: str) -> str:
    """Phase 19 (docs/phase19-audit.md Part 2.2): the real effect of
    activating a prompt version -- prefixed ahead of the existing
    task-specific instruction, never replacing it outright, so the node's
    own task framing (what field to fill, what format is expected) always
    still reaches the LLM even when an operator has set a custom prompt."""
    active = state.active_prompts.get(agent_id)
    return f"{active}\n\n{task_prompt}" if active else task_prompt


async def _node_ceo_kickoff(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    brief = await _llm_or_fallback(
        router,
        "ceo-agent",
        _with_active_prompt(
            state, "ceo-agent", f"Kick off a trading strategy research objective: {state.objective}"
        ),
        fallback={"directive": f"Research and propose a strategy for: {state.objective}"},
    )
    return {"ceo_brief": brief, "node_log": [*state.node_log, "ceo_kickoff"]}


async def _node_market_analysis(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    analysis = {"trend": "neutral", "confidence": 0.5, "objective": state.objective}
    return {"market_analysis": analysis, "node_log": [*state.node_log, "market_analysis"]}


async def _node_strategy_generation(
    state: TradingOSGraphState, router: LlmRouter
) -> dict[str, Any]:
    strategy = await _llm_or_fallback(
        router,
        "strategy-generator",
        _with_active_prompt(
            state,
            "strategy-generator",
            f"Generate a trading strategy given market analysis: {state.market_analysis}",
        ),
        fallback={"name": "fallback-momentum-strategy", "kind": "momentum"},
    )
    return {"strategy": strategy, "node_log": [*state.node_log, "strategy_generation"]}


async def _node_options_strategy(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    legs = {"legs": [], "naked_options_found": False}
    return {"options_legs": legs, "node_log": [*state.node_log, "options_strategy"]}


async def _node_code_generation(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    result = await _llm_or_fallback(
        router,
        "python-code-generator",
        _with_active_prompt(
            state,
            "python-code-generator",
            f"Write a run_backtest(data, config) implementation for strategy: {state.strategy}",
        ),
        fallback={
            "text": "def run_backtest(data, config):\n    return {'trades': 0}\n",
        },
    )
    return {
        "generated_code": result.get("text", ""),
        "node_log": [*state.node_log, "code_generation"],
    }


async def _node_compliance_check(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    verdict = {"blocked": False, "reason": None}
    return {"compliance_verdict": verdict, "node_log": [*state.node_log, "compliance_check"]}


async def _node_code_validation(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    result = code_format_lint({"code": state.generated_code or ""})
    return {
        "validation_result": result,
        "validation_attempts": state.validation_attempts + 1,
        "node_log": [*state.node_log, "code_validation"],
    }


async def _node_backtesting(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    metrics = {"sharpe": 1.0, "max_drawdown": 0.1}
    return {"backtest_metrics": metrics, "node_log": [*state.node_log, "backtesting"]}


async def _node_evaluation(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    sharpe = (state.backtest_metrics or {}).get("sharpe", 0.0)
    verdict = {"verdict": "pass" if sharpe >= 0.5 else "fail", "sharpe": sharpe}
    return {"evaluation_verdict": verdict, "node_log": [*state.node_log, "evaluation"]}


async def _node_optimization(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    result = {"improved": True}
    return {"optimization_result": result, "node_log": [*state.node_log, "optimization"]}


async def _node_risk_assessment(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    result = {"within_limits": True}
    return {"risk_assessment": result, "node_log": [*state.node_log, "risk_assessment"]}


async def _node_deployment(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
    result = {"deployed": True, "target": "paper"}
    return {"deployment_result": result, "node_log": [*state.node_log, "deployment"]}


async def _node_memory_ingest(state: TradingOSGraphState, router: LlmRouter) -> dict[str, Any]:
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
    node_name: str, agent_id: str, event_sink: PipelineEventSink | None = None
) -> Callable[[TradingOSGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: TradingOSGraphState) -> dict[str, Any]:
        await _safe_emit(
            event_sink,
            "step.skipped",
            node=node_name,
            agent_id=agent_id,
            payload={"step_index": len(state.node_log), "reason": "agent-disabled"},
        )
        return {"node_log": [*state.node_log, f"{node_name}:skipped(agent-disabled:{agent_id})"]}

    return node


def build_graph(
    enabled_agents: frozenset[str] | None = None,
    *,
    router: LlmRouter | None = None,
    event_sink: PipelineEventSink | None = None,
) -> CompiledStateGraph[TradingOSGraphState, None, TradingOSGraphState, TradingOSGraphState]:
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
        node: Callable[[TradingOSGraphState], Awaitable[dict[str, Any]]]
        if agent_id in enabled_agents:
            real_fn = NODE_FUNCTIONS[node_name]

            async def timed_node(
                state: TradingOSGraphState,
                _fn: NodeFn = real_fn,
                _name: str = node_name,
                _agent: str = agent_id,
            ) -> dict[str, Any]:
                step_index = len(state.node_log)
                await _safe_emit(
                    event_sink,
                    "step.started",
                    node=_name,
                    agent_id=_agent,
                    payload={"step_index": step_index},
                )
                started = time.perf_counter()
                try:
                    update = await _fn(state, bound_router)
                except Exception as exc:
                    await _safe_emit(
                        event_sink,
                        "step.failed",
                        node=_name,
                        agent_id=_agent,
                        payload={
                            "step_index": step_index,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                            "error": _preview(f"{type(exc).__name__}: {exc}"),
                        },
                    )
                    raise
                finally:
                    agent_node_duration_seconds.labels(node=_name).observe(
                        time.perf_counter() - started
                    )
                await _safe_emit(
                    event_sink,
                    "step.completed",
                    node=_name,
                    agent_id=_agent,
                    payload={
                        "step_index": step_index,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "output": _summarize_output(update),
                    },
                )
                return update

            node = timed_node
        else:
            node = _make_skip_node(node_name, agent_id, event_sink)
        # langgraph's own add_node() overloads are generic over a
        # TypedDict/dataclass/BaseModel-bound NodeInputT and don't cleanly
        # resolve for a plain async Callable[[TradingOSGraphState],
        # Awaitable[dict[str, Any]]] argument -- `node` itself keeps its
        # real, honest type above; only this call into langgraph's own
        # heavily-overloaded API is widened, since correctly modeling those
        # overloads isn't worth the churn for what is, at runtime, this
        # library's own standard "async node function" usage.
        graph.add_node(node_name, cast(Any, node))

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
    active_prompts: dict[str, str] | None = None,
    event_sink: PipelineEventSink | None = None,
) -> TradingOSGraphState:
    compiled = build_graph(enabled_agents, router=router, event_sink=event_sink)
    initial = TradingOSGraphState(
        objective=objective, run_id=run_id, active_prompts=active_prompts or {}
    )
    await _safe_emit(event_sink, "pipeline.started", payload={"objective": _preview(objective)})
    # LangGraph's default recursion_limit (25) is well under the step count
    # a full validator-retry (up to MAX_VALIDATION_ATTEMPTS) plus
    # evaluation-rejection (up to MAX_REJECTIONS full strategy-generation
    # loops, each ~8 nodes) run can legitimately take -- raise it rather
    # than let a real retry sequence be mistaken for a runaway graph.
    try:
        result = await compiled.ainvoke(initial, config={"recursion_limit": 200})
    except Exception as exc:
        await _safe_emit(
            event_sink,
            "pipeline.failed",
            payload={"error": _preview(f"{type(exc).__name__}: {exc}")},
        )
        raise
    final = TradingOSGraphState.model_validate(dict(result))
    await _safe_emit(
        event_sink,
        "pipeline.completed",
        payload={
            "steps": len(final.node_log),
            "rejection_count": final.rejection_count,
            "evaluation_verdict": (final.evaluation_verdict or {}).get("verdict"),
            "deployed": bool(final.deployment_result),
        },
    )
    return final
