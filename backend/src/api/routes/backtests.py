"""Backtest & optimization API routes (Build Spec §10). Phase 5
acceptance: a strategy can be backtested with realistic Indian costs,
walk-forward and Monte Carlo validated, and compared against another run.
"""

import uuid

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    BacktestRunResponse,
    CompareRunsRequest,
    ComparisonResponse,
    MonteCarloRequest,
    OHLCVBar,
    OptimizationRunResponse,
    OptimizeRequest,
    RunBacktestRequest,
    WalkForwardRequest,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.data.freshness_snapshot import DataLakeFreshnessSnapshot, load_freshness_snapshot
from src.engine.backtest.signals import generate_builtin_signals
from src.models.backtest_run import BacktestRun
from src.models.user import User
from src.orchestration import backtests as backtests_orch
from src.orchestration.backtests import BacktestRunNotCompletedError, NoSuchBacktestRunError

router = APIRouter(prefix="/backtests", tags=["backtests"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("POST", "/api/v1/backtests", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/backtests/{run_id}", roles=list(Role))
register_policy("POST", "/api/v1/backtests/{run_id}/walk-forward", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/backtests/{run_id}/monte-carlo", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/backtests/compare", roles=list(Role))
register_policy("POST", "/api/v1/backtests/optimize", roles=_OPERATOR_ROLES)


def _bars_to_df(bars: list[OHLCVBar]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([b.date for b in bars])
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=idx,
    ).sort_index()


def _run_response(run: BacktestRun) -> BacktestRunResponse:
    return BacktestRunResponse(
        id=run.id,
        strategy_version_id=run.strategy_version_id,
        symbol=run.symbol,
        status=run.status,
        refusal_reason=run.refusal_reason,
        metrics=run.metrics,
        walk_forward_result=run.walk_forward_result,
        monte_carlo_result=run.monte_carlo_result,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def run_backtest_endpoint(
    body: RunBacktestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> BacktestRunResponse:
    prices = _bars_to_df(body.bars)
    signals = generate_builtin_signals(prices, body.strategy, body.sma_window)

    # Phase 10: the freshness gate now checks the real ingested data lake
    # (src.data.freshness_snapshot), not a Fake stand-in -- unioned with
    # whatever dates this call's own `body.bars` cover, so a caller who
    # submits explicit bars (the dev/test path every prior phase's tests
    # use) keeps working exactly as before, while a symbol the real
    # pipeline has ingested is now genuinely recognized as fresh even if
    # this call's bars don't happen to include the required date.
    lake_snapshot = await load_freshness_snapshot(db, [body.symbol])
    submitted_dates = frozenset(b.date for b in body.bars)
    combined_dates = lake_snapshot.available_dates.get(body.symbol, frozenset()) | submitted_dates
    data_lake = DataLakeFreshnessSnapshot(available_dates={body.symbol: combined_dates})

    run = await backtests_orch.run_backtest(
        db,
        strategy_version_id=body.strategy_version_id,
        symbol=body.symbol,
        prices=prices,
        signals=signals,
        as_of=body.as_of,
        data_lake=data_lake,
        is_delivery=body.is_delivery,
        initial_capital=body.initial_capital,
        created_by=str(current_user.id),
    )
    return _run_response(run)


@router.get("/{run_id}")
async def get_backtest_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> BacktestRunResponse:
    run = await db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest run not found")
    return _run_response(run)


@router.post("/{run_id}/walk-forward")
async def walk_forward_endpoint(
    run_id: uuid.UUID,
    body: WalkForwardRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> BacktestRunResponse:
    prices = _bars_to_df(body.bars)

    def strategy_fn(_train: pd.DataFrame, test: pd.DataFrame) -> pd.Series:
        return generate_builtin_signals(test, body.strategy, body.sma_window)

    try:
        run = await backtests_orch.run_walk_forward_for_run(
            db,
            run_id,
            prices,
            strategy_fn,
            train_bars=body.train_bars,
            test_bars=body.test_bars,
            step_bars=body.step_bars,
        )
    except NoSuchBacktestRunError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BacktestRunNotCompletedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _run_response(run)


@router.post("/{run_id}/monte-carlo")
async def monte_carlo_endpoint(
    run_id: uuid.UUID,
    body: MonteCarloRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> BacktestRunResponse:
    try:
        run = await backtests_orch.run_monte_carlo_for_run(
            db, run_id, n_paths=body.n_paths, seed=body.seed
        )
    except NoSuchBacktestRunError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BacktestRunNotCompletedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _run_response(run)


@router.post("/compare")
async def compare_endpoint(
    body: CompareRunsRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> ComparisonResponse:
    matrix = await backtests_orch.compare_backtest_runs(db, body.run_ids)
    correlations = {"|".join(sorted(pair)): value for pair, value in matrix.correlations.items()}
    return ComparisonResponse(run_ids=matrix.run_ids, correlations=correlations)


@router.post("/optimize", status_code=status.HTTP_201_CREATED)
async def optimize_endpoint(
    body: OptimizeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> OptimizationRunResponse:
    prices = _bars_to_df(body.bars)

    def param_space_fn(trial):
        return {"window": trial.suggest_int("window", body.sma_window_min, body.sma_window_max)}

    def signal_from_params_fn(prices: pd.DataFrame, params: dict) -> pd.Series:
        sma = prices["close"].rolling(params["window"], min_periods=1).mean()
        return (prices["close"] > sma).astype(int)

    run = await backtests_orch.run_optimization_sweep(
        db,
        strategy_version_id=body.strategy_version_id,
        prices=prices,
        param_space_fn=param_space_fn,
        signal_from_params_fn=signal_from_params_fn,
        n_trials=body.n_trials,
        objective_metric=body.objective_metric,
        seed=body.seed,
        created_by=str(current_user.id),
    )
    return OptimizationRunResponse(
        id=run.id,
        n_trials=run.n_trials,
        best_params=run.best_params,
        best_value=run.best_value,
        param_importance=run.param_importance,
    )
