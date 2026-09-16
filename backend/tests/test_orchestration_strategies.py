"""Strategy pipeline orchestration tests (Build Spec §9): the full
objective -> generated code -> validated -> sandboxed pipeline, and the
Backtesting -> PaperTrading promotion's unbypassability (Phase 2's
approvals mechanism, wired to its concrete case).
"""

import uuid

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.engine.risk.correlation_constraint import make_fake_nifty_benchmark
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.strategy import InstrumentClass, Strategy, StrategyStatus
from src.models.strategy_version import StrategyVersion
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.strategies import (
    PROMOTION_TRANSITION_TYPE,
    CorrelationConstraintBreachedError,
    create_strategy,
    create_version_with_validation,
    promote_to_paper_trading,
    run_strategy_pipeline,
    run_version_in_sandbox,
)
from src.orchestration.transitions import (
    ApprovalGate,
    ApprovalRequiredError,
    conditional_transition,
)


async def test_run_strategy_pipeline_produces_a_validated_sandboxed_strategy(db_session_factory):
    async with db_session_factory() as db:
        strategy, version = await run_strategy_pipeline(
            db,
            name="Momentum",
            objective="Build a momentum strategy for NIFTY",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )

    assert strategy.status == StrategyStatus.BACKTESTING.value
    assert version.static_validation_passed is True
    assert version.sandbox_passed is True
    assert version.sandbox_result["result"] is not None


async def test_status_advances_ideation_to_coding_to_backtesting(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="obj")
        assert strategy.status == StrategyStatus.IDEATION.value

        version = await create_version_with_validation(
            db, strategy, "def run_backtest(data, config):\n    return {}\n"
        )
        assert strategy.status == StrategyStatus.CODING.value

        await run_version_in_sandbox(
            db, strategy, version, sandbox_runtime=RestrictedProcessSandboxRuntime()
        )
        assert strategy.status == StrategyStatus.BACKTESTING.value


async def test_sandbox_refuses_to_run_a_version_that_failed_validation(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="obj")
        bad_version = await create_version_with_validation(db, strategy, "import os\n")
        assert bad_version.static_validation_passed is False

        with pytest.raises(ValueError):
            await run_version_in_sandbox(
                db, strategy, bad_version, sandbox_runtime=RestrictedProcessSandboxRuntime()
            )


async def test_invalid_status_string_rejected_by_db_check_constraint(db_session_factory):
    async with db_session_factory() as db:
        strategy = Strategy(
            name="bad", objective="obj", instrument_class="equity", status="NotARealStatus"
        )
        db.add(strategy)
        with pytest.raises(IntegrityError):
            await db.commit()


async def _make_backtesting_strategy(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db,
            name="Momentum",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    return strategy.id


def _promotion_gate(strategy_id: uuid.UUID) -> ApprovalGate:
    return ApprovalGate(
        subject_type="strategy",
        subject_id=str(strategy_id),
        transition_type=PROMOTION_TRANSITION_TYPE,
    )


async def test_promotion_blocked_with_no_approval_request(db_session_factory):
    strategy_id = await _make_backtesting_strategy(db_session_factory)

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        strategy = await db.get(Strategy, strategy_id)
        assert strategy.status == StrategyStatus.BACKTESTING.value


async def test_promotion_blocked_via_a_different_code_path_too(db_session_factory):
    """Unbypassability: a second, independently-written caller performing
    the exact same gated transition (straight through
    conditional_transition, not through strategies.promote_to_paper_trading)
    is blocked identically -- proving the check lives in the shared
    primitive, not something only promote_to_paper_trading happens to add.
    """
    strategy_id = await _make_backtesting_strategy(db_session_factory)
    gate = _promotion_gate(strategy_id)

    async def _alternate_caller(db) -> bool:
        return await conditional_transition(
            db,
            table=Strategy.__table__,
            id_column=Strategy.id,
            row_id=strategy_id,
            status_column=Strategy.status,
            from_status=StrategyStatus.BACKTESTING.value,
            to_status=StrategyStatus.PAPER_TRADING.value,
            approval=gate,
        )

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _alternate_caller(db)

    async with db_session_factory() as db:
        strategy = await db.get(Strategy, strategy_id)
        assert strategy.status == StrategyStatus.BACKTESTING.value


async def test_promotion_succeeds_once_approved(db_session_factory):
    strategy_id = await _make_backtesting_strategy(db_session_factory)

    async with db_session_factory() as db:
        request = await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        decided = await decide_approval_request(
            db, request.id, approve=True, decided_by="risk-manager-1"
        )
        assert decided.status.value == "approved"

    async with db_session_factory() as db:
        applied = await promote_to_paper_trading(db, strategy_id)
        assert applied is True

    async with db_session_factory() as db:
        strategy = await db.get(Strategy, strategy_id)
        assert strategy.status == StrategyStatus.PAPER_TRADING.value


async def test_rejected_approval_still_blocks_promotion(db_session_factory):
    strategy_id = await _make_backtesting_strategy(db_session_factory)

    async with db_session_factory() as db:
        request = await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )
    async with db_session_factory() as db:
        await decide_approval_request(db, request.id, approve=False, decided_by="risk-manager-1")

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await promote_to_paper_trading(db, strategy_id)


async def test_options_strategy_runs_grounding_and_naked_scan(db_session_factory):
    async with db_session_factory() as db:
        strategy, version = await run_strategy_pipeline(
            db,
            name="Spread",
            objective="obj",
            instrument_class=InstrumentClass.OPTIONS.value,
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )

    assert version.options_legs is not None
    assert "legs" in version.options_legs
    assert "naked_scan_blocked" in version.options_legs


_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def test_version_numbers_increment_per_strategy(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="obj")
        v1 = await create_version_with_validation(db, strategy, _VALID_CODE)
        v2 = await create_version_with_validation(db, strategy, _VALID_CODE)

    assert v1.version_number == 1
    assert v2.version_number == 2


async def test_strategy_versions_are_scoped_per_strategy(db_session_factory):
    async with db_session_factory() as db:
        s1 = await create_strategy(db, name="S1", objective="obj1")
        s2 = await create_strategy(db, name="S2", objective="obj2")
        v1 = await create_version_with_validation(db, s1, _VALID_CODE)
        v2 = await create_version_with_validation(db, s2, _VALID_CODE)

    assert v1.version_number == 1
    assert v2.version_number == 1  # independent per-strategy numbering


async def test_promotion_blocked_by_correlation_constraint_even_when_approved(db_session_factory):
    """Build Spec §8's correlation constraint (src.engine.risk.
    correlation_constraint) is layered on promote_to_paper_trading ahead of
    the approval-gated transition: a strategy whose latest completed
    backtest is correlated with the Nifty 50 benchmark beyond the
    threshold is refused even with a fully approved promotion request.
    """
    strategy_id = await _make_backtesting_strategy(db_session_factory)

    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=0)
    correlated_returns = bench.returns * 0.95 + 0.0001

    async with db_session_factory() as db:
        version_result = await db.execute(
            select(StrategyVersion.id).where(StrategyVersion.strategy_id == strategy_id)
        )
        version_id = version_result.scalar_one()
        db.add(
            BacktestRun(
                strategy_version_id=version_id,
                symbol="DEMO",
                start_date=idx[0].date(),
                end_date=idx[-1].date(),
                status=BacktestStatus.COMPLETED,
                daily_returns=[
                    [ts.isoformat(), float(v)]
                    for ts, v in zip(idx, correlated_returns, strict=True)
                ],
            )
        )
        await db.commit()

        request = await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )

    async with db_session_factory() as db:
        await decide_approval_request(db, request.id, approve=True, decided_by="risk-manager-1")

    async with db_session_factory() as db:
        with pytest.raises(CorrelationConstraintBreachedError):
            await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        strategy = await db.get(Strategy, strategy_id)
        assert strategy.status == StrategyStatus.BACKTESTING.value
