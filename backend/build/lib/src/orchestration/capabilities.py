"""Stub capability registry (Build Spec §7.3, Phase 2).

This phase does NOT include the real 24 agents — every capability here is a
deterministic, dependency-free stand-in so the planner/task-engine/run-control
machinery is fully testable in isolation. Phase 3 replaces this registry's
contents with real agent-backed capabilities; the *shape* (a plain sync
callable taking a params dict and returning a result dict, registered by
name, optionally concurrency-safe) is what later phases build on, not this
module's specific stub logic.

Capabilities are plain synchronous callables (not async) because real
capabilities (backtesting, sandboxed code execution, etc.) are CPU-bound or
blocking; the task engine dispatches them through a thread pool either way
(Build Spec §7.3 "thread-pool dispatch"), so keeping the interface sync here
matches what real capabilities will look like.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

CapabilityFn = Callable[[dict], dict]


class TransientCapabilityError(Exception):
    """Raised by a capability for a failure that's plausibly retryable
    (a stub's stand-in for e.g. a transient network/provider error) —
    feeds the transient-vs-permanent classification run_control.retry_run()
    checks before allowing a retry."""


class PermanentCapabilityError(Exception):
    """Raised by a capability for a failure that will never succeed on
    retry (e.g. malformed input, a structural validation failure).
    Any capability error NOT raised as TransientCapabilityError is treated
    as permanent by default — the conservative choice, since silently
    treating an unclassified failure as safely-retryable would be worse
    than the reverse."""


@dataclass(frozen=True, slots=True)
class FailedCapability:
    transient: bool
    message: str


def _research_scaffold(kind: str) -> CapabilityFn:
    def run(params: dict) -> dict:
        return {"kind": kind, "objective": params.get("objective"), "ok": True}

    return run


def _code_validation(params: dict) -> dict:
    return {"valid": True, "banned_imports_found": []}


def _compliance_check(params: dict) -> dict:
    return {"blocked": False, "reason": None}


def _backtesting(params: dict) -> dict:
    return {"metrics": {"sharpe": 1.0, "max_drawdown": 0.1}}


def _strategy_evaluation(params: dict) -> dict:
    return {"verdict": "pass"}


def _risk_assessment(params: dict) -> dict:
    return {"within_limits": True}


def _stub_noop_concurrent(params: dict) -> dict:
    """Deliberately trivial and side-effect-free so many can run in the
    thread pool at once — see CONCURRENCY_SAFE_CAPABILITIES."""
    return {"ok": True}


def _stub_noop_serial(params: dict) -> dict:
    """Not concurrency-safe: stands in for a capability with shared,
    non-thread-safe state (a real example later: anything touching a
    single sandbox worker slot)."""
    return {"ok": True}


def _stub_slow_concurrent(params: dict) -> dict:
    """Sleeps like a real blocking call would — exists so the task
    engine's thread-pool dispatch can be proven genuinely parallel by wall
    clock time, which the instant stubs above can't demonstrate."""
    time.sleep(params.get("sleep_seconds", 0.3))
    return {"ok": True}


def _stub_slow_serial(params: dict) -> dict:
    """Same shape as _stub_slow_concurrent but NOT concurrency-safe — lets
    a test prove the engine dispatches non-allowlisted capabilities one at
    a time rather than in parallel."""
    time.sleep(params.get("sleep_seconds", 0.3))
    return {"ok": True}


def _stub_always_transient_failure(params: dict) -> dict:
    raise TransientCapabilityError("stub: simulated transient failure")


def _stub_always_permanent_failure(params: dict) -> dict:
    raise PermanentCapabilityError("stub: simulated permanent failure")


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    _fns: dict[str, CapabilityFn] = field(default_factory=dict)
    _concurrency_safe: frozenset[str] = frozenset()

    def get(self, name: str) -> CapabilityFn:
        return self._fns[name]

    def __contains__(self, name: str) -> bool:
        return name in self._fns

    def is_concurrency_safe(self, name: str) -> bool:
        return name in self._concurrency_safe


_FUNCTIONS: dict[str, CapabilityFn] = {
    "research_scaffold_news": _research_scaffold("news"),
    "research_scaffold_sentiment": _research_scaffold("sentiment"),
    "research_scaffold_market_analysis": _research_scaffold("market_analysis"),
    "research_scaffold_portfolio_read": _research_scaffold("portfolio_read"),
    "code_validation": _code_validation,
    "compliance_check": _compliance_check,
    "backtesting": _backtesting,
    "strategy_evaluation": _strategy_evaluation,
    "risk_assessment": _risk_assessment,
    "stub_noop_concurrent": _stub_noop_concurrent,
    "stub_noop_serial": _stub_noop_serial,
    "stub_slow_concurrent": _stub_slow_concurrent,
    "stub_slow_serial": _stub_slow_serial,
    "stub_always_transient_failure": _stub_always_transient_failure,
    "stub_always_permanent_failure": _stub_always_permanent_failure,
}

# Capabilities safe to run in parallel in the task engine's thread pool.
# Everything else is dispatched one at a time. Build Spec §7.3: "thread-pool
# dispatch for an allowlisted set of 'concurrency-safe' capabilities."
CONCURRENCY_SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "research_scaffold_news",
        "research_scaffold_sentiment",
        "research_scaffold_market_analysis",
        "research_scaffold_portfolio_read",
        "stub_noop_concurrent",
        "stub_slow_concurrent",
    }
)

REGISTRY = CapabilityRegistry(_fns=_FUNCTIONS, _concurrency_safe=CONCURRENCY_SAFE_CAPABILITIES)

KNOWN_CAPABILITIES: frozenset[str] = frozenset(_FUNCTIONS)
