"""Walk-Forward Optimization (Build Spec §10): rolling train/test windows;
a strategy must show **positive out-of-sample expectancy in every
window** to pass -- one bad window fails the whole strategy, never
averaged away against good ones. A window that produced no trades at all
cannot be said to have shown positive expectancy either, so it counts as
not passed (`None` expectancy), the same "don't fabricate a result where
there isn't one" posture as the backtest engine's own metrics.

`strategy_fn(train_prices, test_prices) -> test_signals` is the caller's
strategy: fit whatever parameters it wants against the train window, then
return the position-signal series (Build Spec §10 long/flat convention,
see `src.engine.backtest.engine`) to evaluate on the test window. This
engine only owns the windowing and the pass/fail rule, not how a
strategy is fit -- callers range from a fixed rule to a real optimizer.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from src.engine.backtest.engine import Trade, run_vectorized_backtest
from src.engine.backtest.friction import FrictionModel

StrategyFn = Callable[[pd.DataFrame, pd.DataFrame], pd.Series]


def _expectancy(trades: list[Trade]) -> float | None:
    if not trades:
        return None
    # float(...) rather than leaving this as a numpy scalar (net_pnl values
    # flow from pandas/numpy arithmetic) -- a numpy.float64/bool_ downstream
    # (e.g. in `passed` below) isn't JSON-serializable when this result gets
    # persisted (src.orchestration.backtests).
    return float(sum(t.net_pnl for t in trades) / len(trades))


@dataclass(frozen=True, slots=True)
class WindowResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    out_of_sample_expectancy: float | None
    num_trades: int
    passed: bool


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    windows: list[WindowResult]
    passed: bool  # True iff there is at least one window and every one passed


def run_walk_forward(
    prices: pd.DataFrame,
    strategy_fn: StrategyFn,
    *,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
    initial_capital: float = 100_000.0,
    is_delivery: bool = True,
    friction_model: FrictionModel = FrictionModel(),
) -> WalkForwardResult:
    step_bars = step_bars or test_bars
    if train_bars <= 0 or test_bars <= 0 or step_bars <= 0:
        raise ValueError("train_bars, test_bars, and step_bars must all be positive")

    n = len(prices)
    windows: list[WindowResult] = []
    start = 0

    while start + train_bars + test_bars <= n:
        train = prices.iloc[start : start + train_bars]
        test = prices.iloc[start + train_bars : start + train_bars + test_bars]

        test_signals = strategy_fn(train, test)
        result = run_vectorized_backtest(
            test,
            test_signals,
            initial_capital=initial_capital,
            is_delivery=is_delivery,
            friction_model=friction_model,
        )
        expectancy = _expectancy(result.trades)
        passed = expectancy is not None and expectancy > 0

        windows.append(
            WindowResult(
                train_start=train.index[0],
                train_end=train.index[-1],
                test_start=test.index[0],
                test_end=test.index[-1],
                out_of_sample_expectancy=expectancy,
                num_trades=len(result.trades),
                passed=passed,
            )
        )
        start += step_bars

    overall_passed = len(windows) > 0 and all(w.passed for w in windows)
    return WalkForwardResult(windows=windows, passed=overall_passed)
