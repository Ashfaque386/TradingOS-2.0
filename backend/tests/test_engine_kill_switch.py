"""Max Drawdown Kill Switch pure-logic tests (Build Spec §8)."""

from src.engine.risk.kill_switch import evaluate_drawdown


def test_trips_at_exactly_the_threshold():
    result = evaluate_drawdown(current_equity=85_000, peak_equity=100_000, threshold_pct=15.0)
    assert result.drawdown_pct == 15.0
    assert result.should_trip is True


def test_does_not_trip_below_threshold():
    result = evaluate_drawdown(current_equity=90_000, peak_equity=100_000, threshold_pct=15.0)
    assert result.drawdown_pct == 10.0
    assert result.should_trip is False


def test_never_fabricates_drawdown_with_nonpositive_peak():
    result = evaluate_drawdown(current_equity=1_000, peak_equity=0, threshold_pct=15.0)
    assert result.drawdown_pct is None
    assert result.should_trip is False


def test_equity_above_peak_gives_zero_drawdown_not_negative():
    result = evaluate_drawdown(current_equity=110_000, peak_equity=100_000, threshold_pct=15.0)
    assert result.drawdown_pct == 0.0
    assert result.should_trip is False
