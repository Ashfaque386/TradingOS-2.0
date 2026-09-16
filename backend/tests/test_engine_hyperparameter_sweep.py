"""Optuna hyperparameter sweep tests (Build Spec §10): every trial is a
real, complete backtest -- no shortcutting -- and fANOVA importance is
computed from the real completed study.
"""

import numpy as np
import pandas as pd

from src.engine.optimization.hyperparameter_sweep import run_hyperparameter_sweep


def _trending_prices(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    returns = rng.normal(0.0008, 0.012, n)
    close = 100 * np.cumprod(1 + returns)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 30_000,
        },
        index=idx,
    )


def _sma_param_space(trial):
    return {"window": trial.suggest_int("window", 5, 50)}


def _sma_signal(prices: pd.DataFrame, params: dict) -> pd.Series:
    sma = prices["close"].rolling(params["window"], min_periods=1).mean()
    return (prices["close"] > sma).astype(int)


def test_sweep_runs_the_requested_number_of_real_trials():
    prices = _trending_prices()

    result = run_hyperparameter_sweep(prices, _sma_param_space, _sma_signal, n_trials=12, seed=0)

    assert result.n_trials == 12
    assert "window" in result.best_params
    assert 5 <= result.best_params["window"] <= 50


def test_sweep_each_trial_is_a_real_backtest_not_a_shortcut():
    # A spy wraps the real signal function so we can confirm it's called
    # once per trial (i.e. every trial actually ran the engine), not just
    # a subset or a cached/estimated value.
    calls = []

    def spy_signal(prices: pd.DataFrame, params: dict) -> pd.Series:
        calls.append(params["window"])
        return _sma_signal(prices, params)

    prices = _trending_prices()
    result = run_hyperparameter_sweep(prices, _sma_param_space, spy_signal, n_trials=10, seed=1)

    assert len(calls) == 10
    assert len(calls) == result.n_trials


def test_fanova_importance_reports_the_swept_parameter():
    prices = _trending_prices()

    result = run_hyperparameter_sweep(prices, _sma_param_space, _sma_signal, n_trials=15, seed=2)

    assert "window" in result.param_importance
    assert 0.0 <= result.param_importance["window"] <= 1.0 + 1e-9


def test_sweep_never_fabricates_best_value_when_every_trial_is_undefined():
    # A strategy that never trades has an undefined Sharpe every trial --
    # best_value must be None, not a fabricated 0.
    prices = _trending_prices()

    def never_trades(_trial):
        return {}

    def no_signal(prices: pd.DataFrame, _params: dict) -> pd.Series:
        return pd.Series(0, index=prices.index)

    result = run_hyperparameter_sweep(prices, never_trades, no_signal, n_trials=5, seed=0)

    assert result.best_value is None
