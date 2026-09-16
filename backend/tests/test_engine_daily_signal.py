"""Layer 1 entry/exit transition detection tests (Build Spec §11): mirrors
the trusted backtest engine's own one-bar-forward lag exactly.
"""

import pandas as pd

from src.engine.paper_trading.daily_signal import detect_todays_signal


def test_flat_to_long_transition_is_a_buy():
    signals = pd.Series([0, 0, 1, 1])
    result = detect_todays_signal(signals)
    assert result.signal == "BUY"
    assert result.position_today == 1
    assert result.position_yesterday == 0


def test_long_to_flat_transition_is_a_sell():
    signals = pd.Series([1, 1, 0, 0])
    result = detect_todays_signal(signals)
    assert result.signal == "SELL"
    assert result.position_today == 0
    assert result.position_yesterday == 1


def test_no_transition_when_position_unchanged():
    signals = pd.Series([1, 1, 1, 1])
    result = detect_todays_signal(signals)
    assert result.signal is None


def test_too_short_a_series_never_fabricates_a_signal():
    result = detect_todays_signal(pd.Series([1]))
    assert result.signal is None
    result_empty = detect_todays_signal(pd.Series([], dtype=int))
    assert result_empty.signal is None


def test_matches_the_backtest_engines_own_lag_convention():
    """The engine computes position = signals.shift(1).fillna(0).astype(int)
    -- this test proves detect_todays_signal reads the identical rule,
    not a second independently-written lag that could drift."""
    signals = pd.Series([0, 1, 1, 0])
    expected_position = signals.shift(1).fillna(0).astype(int)
    result = detect_todays_signal(signals)
    assert result.position_today == expected_position.iloc[-1]
    assert result.position_yesterday == expected_position.iloc[-2]
