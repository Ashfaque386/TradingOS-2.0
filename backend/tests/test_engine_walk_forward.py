"""Walk-Forward Optimization tests (Build Spec §10): a strategy must show
positive out-of-sample expectancy in *every* window to pass -- one bad
window fails the whole thing, never averaged away.
"""

import numpy as np
import pandas as pd

from src.engine.optimization.walk_forward import run_walk_forward


def _trending_prices(n: int, *, daily_return: float) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = 100 * np.cumprod([1 + daily_return] * n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 50_000,
        },
        index=idx,
    )


def _always_long(_train: pd.DataFrame, test: pd.DataFrame) -> pd.Series:
    return pd.Series(1, index=test.index)


def test_walk_forward_passes_when_every_window_is_positive():
    prices = _trending_prices(150, daily_return=0.003)  # steady uptrend throughout

    result = run_walk_forward(prices, _always_long, train_bars=30, test_bars=20)

    assert len(result.windows) > 0
    assert all(w.passed for w in result.windows)
    assert result.passed is True


def test_walk_forward_fails_overall_if_a_single_window_is_negative():
    # Strong uptrend for the first ~100 bars, then a sharp reversal --
    # the always-long strategy must fail on the reversal window even
    # though earlier windows were strongly positive.
    n_up, n_down = 100, 60
    up = 100 * np.cumprod([1.004] * n_up)
    down = up[-1] * np.cumprod([0.996] * n_down)
    close = np.concatenate([up, down])
    idx = pd.bdate_range("2024-01-01", periods=len(close))
    prices = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 50_000,
        },
        index=idx,
    )

    result = run_walk_forward(prices, _always_long, train_bars=30, test_bars=25, step_bars=25)

    assert any(not w.passed for w in result.windows)
    assert result.passed is False


def test_walk_forward_window_with_no_trades_counts_as_not_passed():
    prices = _trending_prices(100, daily_return=0.002)

    def _never_trades(_train: pd.DataFrame, test: pd.DataFrame) -> pd.Series:
        return pd.Series(0, index=test.index)

    result = run_walk_forward(prices, _never_trades, train_bars=30, test_bars=20)

    assert len(result.windows) > 0
    assert all(w.out_of_sample_expectancy is None for w in result.windows)
    assert all(not w.passed for w in result.windows)
    assert result.passed is False


def test_walk_forward_produces_no_windows_when_data_is_too_short():
    prices = _trending_prices(40, daily_return=0.001)

    result = run_walk_forward(prices, _always_long, train_bars=30, test_bars=20)

    assert result.windows == []
    assert result.passed is False  # no windows at all is not a pass
