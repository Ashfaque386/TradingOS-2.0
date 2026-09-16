"""Backtest orchestration tests (Build Spec §10): the freshness gate
refuses stale data end-to-end (persisted, engine never runs), and the
full run -> walk-forward -> Monte Carlo -> compare pipeline round-trips
through the DB.
"""

import numpy as np
import pandas as pd
import pytest

from src.engine.backtest.freshness import FakeDataLakeFreshness, previous_trading_day
from src.models.backtest_run import BacktestStatus
from src.orchestration.backtests import (
    BacktestRunNotCompletedError,
    NoSuchBacktestRunError,
    compare_backtest_runs,
    run_backtest,
    run_monte_carlo_for_run,
    run_optimization_sweep,
    run_walk_forward_for_run,
)
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


def _trending_prices(n: int = 200, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    returns = rng.normal(0.0006, 0.012, n)
    close = 100 * np.cumprod(1 + returns)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 25_000,
        },
        index=idx,
    )


async def _make_strategy_version(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
    return version.id


async def test_stale_data_is_refused_and_the_engine_never_runs(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()
    signals = pd.Series(1, index=prices.index)
    as_of = (prices.index[-1] + pd.Timedelta(days=1)).date()
    empty_data_lake = FakeDataLakeFreshness(available_dates={})

    async with db_session_factory() as db:
        run = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals,
            as_of=as_of,
            data_lake=empty_data_lake,
        )

    assert run.status == BacktestStatus.REFUSED_STALE_DATA
    assert run.metrics is None
    assert run.trade_pnls is None
    assert run.refusal_reason is not None


async def test_fresh_data_runs_the_real_engine_and_persists_metrics(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()
    sma = prices["close"].rolling(15, min_periods=1).mean()
    signals = (prices["close"] > sma).astype(int)
    as_of = (prices.index[-1] + pd.Timedelta(days=1)).date()
    required = previous_trading_day(as_of)
    data_lake = FakeDataLakeFreshness(available_dates={"TESTSTOCK": frozenset({required})})

    async with db_session_factory() as db:
        run = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals,
            as_of=as_of,
            data_lake=data_lake,
        )

    assert run.status == BacktestStatus.COMPLETED
    assert run.metrics is not None
    assert "sharpe" in run.metrics
    assert run.trade_pnls is not None


async def test_walk_forward_and_monte_carlo_attach_to_the_same_run(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()
    sma = prices["close"].rolling(15, min_periods=1).mean()
    signals = (prices["close"] > sma).astype(int)
    as_of = (prices.index[-1] + pd.Timedelta(days=1)).date()
    required = previous_trading_day(as_of)
    data_lake = FakeDataLakeFreshness(available_dates={"TESTSTOCK": frozenset({required})})

    async with db_session_factory() as db:
        run = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals,
            as_of=as_of,
            data_lake=data_lake,
        )
        run_id = run.id

    def always_long(_train, test):
        return pd.Series(1, index=test.index)

    async with db_session_factory() as db:
        run = await run_walk_forward_for_run(
            db, run_id, prices, always_long, train_bars=40, test_bars=20
        )
    assert run.walk_forward_result is not None
    assert "passed" in run.walk_forward_result

    async with db_session_factory() as db:
        run = await run_monte_carlo_for_run(db, run_id, n_paths=500, seed=1)
    assert run.monte_carlo_result is not None
    assert "percentile_95_max_drawdown" in run.monte_carlo_result


async def test_walk_forward_on_a_refused_run_raises(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()
    signals = pd.Series(1, index=prices.index)
    as_of = (prices.index[-1] + pd.Timedelta(days=1)).date()
    empty_data_lake = FakeDataLakeFreshness(available_dates={})

    async with db_session_factory() as db:
        run = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals,
            as_of=as_of,
            data_lake=empty_data_lake,
        )
        run_id = run.id

    def always_long(_train, test):
        return pd.Series(1, index=test.index)

    async with db_session_factory() as db:
        with pytest.raises(BacktestRunNotCompletedError):
            await run_walk_forward_for_run(
                db, run_id, prices, always_long, train_bars=40, test_bars=20
            )


async def test_monte_carlo_on_unknown_run_raises(db_session_factory):
    import uuid

    async with db_session_factory() as db:
        with pytest.raises(NoSuchBacktestRunError):
            await run_monte_carlo_for_run(db, uuid.uuid4())


async def test_compare_backtest_runs_across_two_real_runs(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()
    as_of = (prices.index[-1] + pd.Timedelta(days=1)).date()
    required = previous_trading_day(as_of)
    data_lake = FakeDataLakeFreshness(available_dates={"TESTSTOCK": frozenset({required})})

    sma = prices["close"].rolling(15, min_periods=1).mean()
    signals_a = (prices["close"] > sma).astype(int)
    signals_b = 1 - signals_a

    async with db_session_factory() as db:
        run_a = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals_a,
            as_of=as_of,
            data_lake=data_lake,
        )
        run_b = await run_backtest(
            db,
            strategy_version_id=version_id,
            symbol="TESTSTOCK",
            prices=prices,
            signals=signals_b,
            as_of=as_of,
            data_lake=data_lake,
        )

    async with db_session_factory() as db:
        matrix = await compare_backtest_runs(db, [run_a.id, run_b.id])

    correlation = matrix.get(str(run_a.id), str(run_b.id))
    assert correlation is not None
    assert -1.0 <= correlation <= 1.0


async def test_optimization_sweep_persists_real_results(db_session_factory):
    version_id = await _make_strategy_version(db_session_factory)
    prices = _trending_prices()

    def param_space(trial):
        return {"window": trial.suggest_int("window", 5, 40)}

    def signal_fn(prices: pd.DataFrame, params: dict) -> pd.Series:
        sma = prices["close"].rolling(params["window"], min_periods=1).mean()
        return (prices["close"] > sma).astype(int)

    async with db_session_factory() as db:
        run = await run_optimization_sweep(
            db,
            strategy_version_id=version_id,
            prices=prices,
            param_space_fn=param_space,
            signal_from_params_fn=signal_fn,
            n_trials=10,
            seed=0,
        )

    assert run.n_trials == 10
    assert run.best_params is not None
    assert "window" in run.param_importance
