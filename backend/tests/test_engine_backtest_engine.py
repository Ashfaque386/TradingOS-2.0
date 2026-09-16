"""Vectorized backtest engine tests (Build Spec §10): never fabricates a
metric (non-finite -> None, not 0/a default), and a hand-traceable single
trade's net P&L matches the friction model's own cost computation exactly
-- proving the engine wires turnover/is_buy/is_delivery through to
compute_leg_costs correctly, not just that the formula itself is right
(that's friction.py's own test file).
"""

import pandas as pd
import pytest

from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.friction import FrictionModel, compute_leg_costs


def _flat_prices(n: int, opens: list[float] | None = None) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=n)
    values = opens or [100.0] * n
    return pd.DataFrame(
        {
            "open": values,
            "high": [v + 1 for v in values],
            "low": [v - 1 for v in values],
            "close": values,
            "volume": [100_000] * n,
        },
        index=idx,
    )


def test_zero_signal_never_trades_and_never_fabricates_metrics():
    prices = _flat_prices(20)
    signals = pd.Series(0, index=prices.index)

    result = run_vectorized_backtest(prices, signals)

    assert result.metrics.num_trades == 0
    assert result.metrics.sharpe is None  # zero-variance returns -> undefined
    assert result.metrics.win_rate is None  # no trades -> undefined
    assert result.metrics.profit_factor is None  # no losses to divide by
    assert result.metrics.max_drawdown == 0.0  # genuinely zero, a real number
    assert result.metrics.total_return == 0.0


def test_single_trade_net_pnl_matches_friction_model_exactly():
    # No slippage (multiplier/coefficient zeroed) so the trade prices are
    # exactly the bar opens -- makes this fully hand-traceable.
    opens = [100.0, 100.0, 150.0, 160.0, 170.0, 180.0]
    prices = _flat_prices(6, opens)
    signals = pd.Series([0, 1, 1, 0, 0, 0], index=prices.index)
    friction_model = FrictionModel(atr_slippage_multiplier=0.0, volume_impact_coefficient=0.0)

    result = run_vectorized_backtest(
        prices, signals, initial_capital=100_000.0, is_delivery=True, friction_model=friction_model
    )

    assert len(result.trades) == 1
    trade = result.trades[0]

    # Signal at bar 1 (value 1) is acted on at bar 2's open (100 -> shift);
    # signal drops at bar 3, acted on at bar 4's open.
    assert trade.entry_price == pytest.approx(150.0)
    assert trade.exit_price == pytest.approx(170.0)

    quantity = int(100_000 // 150.0)
    assert trade.quantity == quantity

    entry_costs = compute_leg_costs(
        turnover=quantity * 150.0, is_buy=True, is_delivery=True, model=friction_model
    )
    exit_costs = compute_leg_costs(
        turnover=quantity * 170.0, is_buy=False, is_delivery=True, model=friction_model
    )
    expected_gross = quantity * (170.0 - 150.0)
    expected_net = expected_gross - entry_costs.total - exit_costs.total

    assert trade.gross_pnl == pytest.approx(expected_gross)
    assert trade.net_pnl == pytest.approx(expected_net)


def test_open_position_at_end_of_window_is_closed_at_final_close():
    opens = [100.0, 100.0, 150.0, 160.0, 170.0]
    prices = _flat_prices(5, opens)
    prices.loc[prices.index[-1], "close"] = 999.0  # distinct from any open
    signals = pd.Series([0, 1, 1, 1, 1], index=prices.index)  # never exits

    result = run_vectorized_backtest(prices, signals)

    assert len(result.trades) == 1
    # exit priced off the final bar's close (mark-to-market), not an open.
    assert result.trades[0].exit_price < 999.0  # slippage worsens a sell
    assert result.trades[0].exit_date == prices.index[-1]


def test_non_finite_metrics_are_none_not_a_default():
    # A single bar can't produce a meaningful CAGR/Sharpe -- confirm the
    # engine reports that honestly rather than defaulting to 0.
    prices = _flat_prices(1)
    signals = pd.Series([0], index=prices.index)

    result = run_vectorized_backtest(prices, signals)

    assert result.metrics.cagr is None
    assert result.metrics.sharpe is None


def test_trades_are_long_only_shifted_one_bar_to_avoid_lookahead():
    prices = _flat_prices(5, [100.0, 200.0, 300.0, 100.0, 100.0])
    # Signal flips to 1 on bar 0 -- must NOT be actable on bar 0's own open.
    signals = pd.Series([1, 1, 0, 0, 0], index=prices.index)

    result = run_vectorized_backtest(prices, signals)

    assert len(result.trades) == 1
    assert result.trades[0].entry_date == prices.index[1]  # not bar 0


def test_no_trade_when_capital_cannot_afford_a_single_share():
    prices = _flat_prices(5, [1_000_000.0] * 5)
    signals = pd.Series([0, 1, 1, 0, 0], index=prices.index)

    result = run_vectorized_backtest(prices, signals, initial_capital=100.0)

    assert result.metrics.num_trades == 0
