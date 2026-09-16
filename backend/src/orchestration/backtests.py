"""Thin persistence layer wiring the pure `src.engine.backtest`/
`src.engine.optimization` functions (Build Spec §10) to the DB:
`BacktestRun`/`OptimizationRun` rows against a `StrategyVersion`. All
statistical/computational logic lives in the pure engine modules -- this
module only owns the data-freshness gate check, invoking the engine, and
persisting results.

A `BacktestRun` here is the platform's own authoritative, real-cost-
modeled backtest -- distinct from a `StrategyVersion`'s self-reported
`sandbox_result` (Phase 4, the untrusted strategy code's own claim about
itself). Walk-forward and Monte Carlo attach their results onto an
existing `COMPLETED` run rather than creating new runs, since they're
further analysis of the same underlying trade sequence, not a new
backtest.
"""

import uuid
from dataclasses import asdict
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.engine.backtest.comparison import ComparisonMatrix, compare_runs
from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.freshness import DataLakeFreshnessCheck, check_data_freshness
from src.engine.backtest.friction import FrictionModel
from src.engine.optimization.hyperparameter_sweep import (
    ParamSpaceFn,
    SignalFromParamsFn,
    run_hyperparameter_sweep,
)
from src.engine.optimization.monte_carlo import run_monte_carlo
from src.engine.optimization.walk_forward import StrategyFn, run_walk_forward
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.optimization_run import OptimizationRun


class NoSuchBacktestRunError(Exception):
    pass


class BacktestRunNotCompletedError(Exception):
    """Raised when walk-forward/Monte Carlo is requested against a run
    that never produced a real trade sequence (refused for stale data, or
    failed) -- there is nothing real to analyze further.
    """


async def run_backtest(
    db: AsyncSession,
    *,
    strategy_version_id: uuid.UUID,
    symbol: str,
    prices: pd.DataFrame,
    signals: pd.Series,
    as_of: date,
    data_lake: DataLakeFreshnessCheck,
    friction_model: FrictionModel = FrictionModel(),
    initial_capital: float = 100_000.0,
    is_delivery: bool = True,
    created_by: str | None = None,
) -> BacktestRun:
    freshness = check_data_freshness(symbol=symbol, as_of=as_of, data_lake=data_lake)
    if not freshness.fresh:
        run = BacktestRun(
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            start_date=prices.index[0].date(),
            end_date=prices.index[-1].date(),
            status=BacktestStatus.REFUSED_STALE_DATA,
            refusal_reason=freshness.reason,
            created_by=created_by,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    result = run_vectorized_backtest(
        prices,
        signals,
        initial_capital=initial_capital,
        is_delivery=is_delivery,
        friction_model=friction_model,
    )

    run = BacktestRun(
        strategy_version_id=strategy_version_id,
        symbol=symbol,
        start_date=prices.index[0].date(),
        end_date=prices.index[-1].date(),
        status=BacktestStatus.COMPLETED,
        friction_config=asdict(friction_model),
        metrics=asdict(result.metrics),
        daily_returns=[[ts.isoformat(), float(v)] for ts, v in result.daily_returns.items()],
        trade_pnls=[float(t.net_pnl) for t in result.trades],
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _get_completed_run(db: AsyncSession, backtest_run_id: uuid.UUID) -> BacktestRun:
    run = await db.get(BacktestRun, backtest_run_id)
    if run is None:
        raise NoSuchBacktestRunError(f"no such backtest run: {backtest_run_id}")
    if run.status != BacktestStatus.COMPLETED:
        raise BacktestRunNotCompletedError(
            f"backtest run {backtest_run_id} is {run.status!r}, not completed -- "
            "nothing real to analyze further"
        )
    return run


async def run_walk_forward_for_run(
    db: AsyncSession,
    backtest_run_id: uuid.UUID,
    prices: pd.DataFrame,
    strategy_fn: StrategyFn,
    *,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> BacktestRun:
    run = await _get_completed_run(db, backtest_run_id)
    friction_model = (
        FrictionModel(**run.friction_config) if run.friction_config else FrictionModel()
    )

    wf_result = run_walk_forward(
        prices,
        strategy_fn,
        train_bars=train_bars,
        test_bars=test_bars,
        step_bars=step_bars,
        friction_model=friction_model,
    )

    run.walk_forward_result = {
        "passed": wf_result.passed,
        "windows": [
            {
                "train_start": w.train_start.isoformat(),
                "train_end": w.train_end.isoformat(),
                "test_start": w.test_start.isoformat(),
                "test_end": w.test_end.isoformat(),
                "out_of_sample_expectancy": w.out_of_sample_expectancy,
                "num_trades": w.num_trades,
                "passed": w.passed,
            }
            for w in wf_result.windows
        ],
    }
    await db.commit()
    await db.refresh(run)
    return run


async def run_monte_carlo_for_run(
    db: AsyncSession,
    backtest_run_id: uuid.UUID,
    *,
    n_paths: int = 10_000,
    seed: int | None = None,
) -> BacktestRun:
    run = await _get_completed_run(db, backtest_run_id)
    mc_result = run_monte_carlo(run.trade_pnls or [], n_paths=n_paths, seed=seed)
    run.monte_carlo_result = asdict(mc_result)
    await db.commit()
    await db.refresh(run)
    return run


async def run_optimization_sweep(
    db: AsyncSession,
    *,
    strategy_version_id: uuid.UUID,
    prices: pd.DataFrame,
    param_space_fn: ParamSpaceFn,
    signal_from_params_fn: SignalFromParamsFn,
    n_trials: int = 50,
    objective_metric: str = "sharpe",
    friction_model: FrictionModel = FrictionModel(),
    seed: int | None = None,
    created_by: str | None = None,
) -> OptimizationRun:
    sweep_result = run_hyperparameter_sweep(
        prices,
        param_space_fn,
        signal_from_params_fn,
        n_trials=n_trials,
        objective_metric=objective_metric,
        friction_model=friction_model,
        seed=seed,
    )

    run = OptimizationRun(
        strategy_version_id=strategy_version_id,
        objective_metric=objective_metric,
        n_trials=sweep_result.n_trials,
        best_params=sweep_result.best_params,
        best_value=sweep_result.best_value,
        param_importance=sweep_result.param_importance,
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def compare_backtest_runs(db: AsyncSession, run_ids: list[uuid.UUID]) -> ComparisonMatrix:
    result = await db.execute(select(BacktestRun).where(BacktestRun.id.in_(run_ids)))
    runs = {str(r.id): r for r in result.scalars()}

    daily_returns_by_run: dict[str, pd.Series] = {}
    for run_id in run_ids:
        run = runs.get(str(run_id))
        if run is None or not run.daily_returns:
            continue
        dates = [pd.Timestamp(d) for d, _ in run.daily_returns]
        values = [v for _, v in run.daily_returns]
        daily_returns_by_run[str(run_id)] = pd.Series(values, index=pd.DatetimeIndex(dates))

    return compare_runs(daily_returns_by_run)
