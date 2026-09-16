"""Strategy pipeline driver (Build Spec §9): objective -> generated code ->
static validation -> sandbox execution -> (options only) leg grounding +
naked-options scan -> Backtesting, then an approval-gated promotion to
PaperTrading.

Code generation goes through the LLM router (src.agents.llm_router,
Phase 3) with the same "real call, honest deterministic fallback when no
provider is reachable" posture used throughout this codebase -- this
sandbox has no provider keys/egress, so every generated module here comes
from the fallback path, clearly logged as such.

The one transition this module treats specially is Backtesting ->
PaperTrading: it goes through src.orchestration.transitions.
conditional_transition() with an ApprovalGate (Phase 2's generic approvals
mechanism, built exactly for this concrete case) rather than a plain
UPDATE. That's what makes it provably unbypassable (see
tests/test_orchestration_strategies.py) -- every other status write in
this module is a plain, sequential, non-gated pipeline advance.

Build Spec §8's correlation constraint is layered on top of that same
`promote_to_paper_trading` entrypoint: if the strategy's most recent
COMPLETED backtest (Phase 5) has daily returns on record, they're checked
against a Nifty 50 benchmark (src.engine.risk.correlation_constraint)
*before* conditional_transition is even attempted -- a breach never
touches the approval-gated transition at all. A strategy with no completed
backtest returns yet has nothing to evaluate (never fabricated), so the
check is a no-op for it, same as Phase 4's own tests that promote a
strategy with no Phase 5 backtest ever attached. The exact same
`evaluate_correlation_constraint` function is also called from
src.orchestration.risk_gate's per-tick order-intent gate -- deliberately
one function, two call sites, not two implementations that could drift
apart (Build Spec §8 flags this as a gap a prior build left open).
"""

import uuid
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.core.config import get_settings
from src.engine.options_grounding import (
    GroundedLeg,
    ground_option_legs,
    naked_options_scan,
)
from src.engine.risk.correlation_constraint import (
    DEFAULT_CORRELATION_THRESHOLD,
    NiftyBenchmarkProvider,
    evaluate_correlation_constraint,
    make_fake_nifty_benchmark,
)
from src.engine.sandbox.factory import SandboxRuntime, get_default_sandbox_runtime
from src.engine.sandbox.types import SandboxLimits
from src.engine.validation import validate_strategy_code
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.strategy import InstrumentClass, Strategy, StrategyStatus
from src.models.strategy_version import StrategyVersion
from src.orchestration.transitions import ApprovalGate, conditional_transition

PROMOTION_TRANSITION_TYPE = "backtesting_to_papertrading"


class CorrelationConstraintBreachedError(Exception):
    """Raised by promote_to_paper_trading when the strategy's backtest
    returns are correlated with the Nifty 50 benchmark beyond the
    constraint threshold -- promotion is refused before the approval-gated
    transition is even attempted."""

    def __init__(self, strategy_id: uuid.UUID, correlation: float, threshold: float):
        self.strategy_id = strategy_id
        self.correlation = correlation
        self.threshold = threshold
        super().__init__(
            f"strategy {strategy_id} backtest returns are correlated {correlation:.4f} "
            f"with the Nifty 50 benchmark, exceeding the {threshold:.4f} constraint -- "
            "promotion refused"
        )


_FALLBACK_CODE_TEMPLATE = '''\
def run_backtest(data, config):
    """Deterministic fallback strategy body (Build Spec §9's fixed
    run_backtest(data, config) -> dict contract) -- used when no LLM
    provider answered.

    Objective: {objective_summary}
    """
    trades = len(data) if hasattr(data, "__len__") else 0
    return {{"trades": trades, "sharpe": 1.0, "max_drawdown": 0.1}}
'''


def _fallback_code(objective: str) -> str:
    # Kept short enough that "    Objective: {summary}" (the longest line
    # this template produces) never trips ruff's E501 line-length check --
    # the fallback code has to pass the same static validation as any
    # real generated module.
    summary = objective.replace('"', "'").replace("\n", " ")[:70]
    return _FALLBACK_CODE_TEMPLATE.format(objective_summary=summary)


async def generate_strategy_code(objective: str, *, router: LlmRouter | None = None) -> str:
    router = router or get_llm_router()
    try:
        result = await router.complete(
            agent_id="python-code-generator",
            prompt=f"Write a run_backtest(data, config) -> dict implementation for: {objective}",
        )
        return result.text
    except LlmRouterExhaustedError:
        return _fallback_code(objective)


async def create_strategy(
    db: AsyncSession,
    *,
    name: str,
    objective: str,
    instrument_class: str = InstrumentClass.EQUITY.value,
    created_by: str | None = None,
) -> Strategy:
    strategy = Strategy(
        name=name,
        objective=objective,
        instrument_class=instrument_class,
        status=StrategyStatus.IDEATION.value,
        created_by=created_by,
    )
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return strategy


async def _next_version_number(db: AsyncSession, strategy_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(StrategyVersion.version_number), 0) + 1).where(
            StrategyVersion.strategy_id == strategy_id
        )
    )
    return result.scalar_one()


def _default_option_legs(objective: str) -> list[dict]:
    """Deterministic fallback legs (no real Options Strategy Agent LLM
    output parser yet) -- a simple bull call spread, grounded and scanned
    just like a real proposal would be. Real leg proposals arrive the same
    way generated code does: via the LLM router, wired in once the
    Options Strategy Agent's structured-output contract is built out.
    """
    return [
        {"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50},
        {"option_type": "CE", "strike": 20300, "action": "buy", "qty": 50},
    ]


async def create_version_with_validation(
    db: AsyncSession,
    strategy: Strategy,
    code: str,
    *,
    created_by: str | None = None,
) -> StrategyVersion:
    validation = validate_strategy_code(code)
    version_number = await _next_version_number(db, strategy.id)

    version = StrategyVersion(
        strategy_id=strategy.id,
        version_number=version_number,
        code=code,
        static_validation_passed=validation.passed,
        static_validation_errors=validation.errors or None,
        created_by=created_by,
    )
    db.add(version)

    # Coding once code exists at all; Ideation is "no code yet" only.
    if strategy.status == StrategyStatus.IDEATION.value:
        await db.execute(
            update(Strategy)
            .where(Strategy.id == strategy.id, Strategy.status == StrategyStatus.IDEATION.value)
            .values(status=StrategyStatus.CODING.value)
        )
        strategy.status = StrategyStatus.CODING.value

    await db.commit()
    await db.refresh(version)
    return version


async def run_version_in_sandbox(
    db: AsyncSession,
    strategy: Strategy,
    version: StrategyVersion,
    *,
    data: list | dict | None = None,
    config: dict | None = None,
    sandbox_runtime: SandboxRuntime | None = None,
    scratch_dir: Path | None = None,
) -> StrategyVersion:
    if not version.static_validation_passed:
        raise ValueError(
            f"strategy version {version.id} failed static validation "
            f"({version.static_validation_errors!r}); refusing to run it in the sandbox"
        )

    settings = get_settings()
    runtime = sandbox_runtime or get_default_sandbox_runtime()
    scratch = scratch_dir or Path(f"/tmp/tradingos-sandbox/{version.id}")
    data_dir = Path(settings.data_lake_path)

    result = runtime.execute(
        version.code,
        params={"data": data if data is not None else [], "config": config or {}},
        scratch_dir=scratch,
        data_dir=data_dir,
        limits=SandboxLimits(
            cpu_seconds=settings.sandbox_cpu_seconds,
            memory_bytes=settings.sandbox_memory_bytes,
            timeout_seconds=settings.sandbox_default_timeout_seconds,
        ),
    )

    version.sandbox_passed = result.success
    version.sandbox_result = {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_seconds": result.duration_seconds,
    }

    if strategy.instrument_class == InstrumentClass.OPTIONS.value:
        legs = _default_option_legs(strategy.objective)
        grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
        scan = naked_options_scan(grounded)
        version.options_legs = {
            "legs": [_leg_to_dict(leg) for leg in grounded],
            "naked_scan_blocked": scan.blocked,
            "naked_scan_reasons": scan.reasons,
        }
        if scan.blocked:
            version.sandbox_passed = False

    # Backtesting once a version has statically validated code and has at
    # least attempted a sandbox run -- regardless of the run's own
    # pass/fail (a failing backtest is still "in Backtesting", same as a
    # failing test run doesn't kick a PR back out of "in review").
    if strategy.status == StrategyStatus.CODING.value:
        await db.execute(
            update(Strategy)
            .where(Strategy.id == strategy.id, Strategy.status == StrategyStatus.CODING.value)
            .values(status=StrategyStatus.BACKTESTING.value)
        )
        strategy.status = StrategyStatus.BACKTESTING.value

    await db.commit()
    await db.refresh(version)
    return version


def _leg_to_dict(leg: GroundedLeg) -> dict:
    return {
        "option_type": leg.option_type,
        "strike": leg.strike,
        "expiry": leg.expiry,
        "action": leg.action,
        "qty": leg.qty,
        "ltp": leg.ltp,
        "grounded": leg.grounded,
    }


async def run_strategy_pipeline(
    db: AsyncSession,
    *,
    name: str,
    objective: str,
    instrument_class: str = InstrumentClass.EQUITY.value,
    created_by: str | None = None,
    router: LlmRouter | None = None,
    sandbox_runtime: SandboxRuntime | None = None,
) -> tuple[Strategy, StrategyVersion]:
    """The synchronous, one-shot "submit an objective, get a strategy back"
    entrypoint (mirrors Phase 2/3's own "create and drive to completion"
    endpoints) -- create -> generate -> validate -> (if valid) sandbox run
    -> (if options) ground legs + naked scan. Never raises for a strategy
    that fails validation or the sandbox -- that's a normal, inspectable
    outcome (version.static_validation_passed / .sandbox_passed), not an
    exception; it only raises for a genuine infra failure.
    """
    strategy = await create_strategy(
        db, name=name, objective=objective, instrument_class=instrument_class, created_by=created_by
    )
    code = await generate_strategy_code(objective, router=router)
    version = await create_version_with_validation(db, strategy, code, created_by=created_by)

    if version.static_validation_passed:
        version = await run_version_in_sandbox(
            db, strategy, version, sandbox_runtime=sandbox_runtime
        )

    return strategy, version


async def _latest_completed_backtest_returns(
    db: AsyncSession, strategy_id: uuid.UUID
) -> pd.Series | None:
    version_result = await db.execute(
        select(StrategyVersion.id)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_number.desc())
        .limit(1)
    )
    version_id = version_result.scalar_one_or_none()
    if version_id is None:
        return None

    run_result = await db.execute(
        select(BacktestRun)
        .where(
            BacktestRun.strategy_version_id == version_id,
            BacktestRun.status == BacktestStatus.COMPLETED,
        )
        .order_by(BacktestRun.created_at.desc())
        .limit(1)
    )
    run = run_result.scalar_one_or_none()
    if run is None or not run.daily_returns:
        return None

    index = pd.DatetimeIndex([pd.Timestamp(ts) for ts, _ in run.daily_returns])
    values = [value for _, value in run.daily_returns]
    return pd.Series(values, index=index)


async def promote_to_paper_trading(
    db: AsyncSession,
    strategy_id: uuid.UUID,
    *,
    benchmark_provider: NiftyBenchmarkProvider | None = None,
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> bool:
    """Backtesting -> PaperTrading, gated by Phase 2's generic approvals
    mechanism (src.orchestration.transitions.conditional_transition +
    ApprovalGate) -- the concrete case that primitive was built for. No
    other function in this codebase performs this transition any other
    way; see tests/test_orchestration_strategies.py for the unbypassability
    proof (a second, independently-written caller of conditional_transition
    with the same gate is blocked identically).

    Layered on top: Build Spec §8's correlation constraint (module
    docstring). `benchmark_provider` defaults to a deterministic seeded
    fake Nifty 50 series (the real index feed is a Phase 10 concern) so the
    check is genuinely active by default, not opt-in -- pass a real
    provider once Phase 10 ships one.
    """
    returns = await _latest_completed_backtest_returns(db, strategy_id)
    if returns is not None:
        provider = benchmark_provider or make_fake_nifty_benchmark(returns.index, seed=0)
        result = evaluate_correlation_constraint(
            returns, provider.daily_returns(), threshold=correlation_threshold
        )
        if result.breached:
            raise CorrelationConstraintBreachedError(
                strategy_id, result.correlation, correlation_threshold
            )

    return await conditional_transition(
        db,
        table=Strategy.__table__,
        id_column=Strategy.id,
        row_id=strategy_id,
        status_column=Strategy.status,
        from_status=StrategyStatus.BACKTESTING.value,
        to_status=StrategyStatus.PAPER_TRADING.value,
        approval=ApprovalGate(
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        ),
    )
