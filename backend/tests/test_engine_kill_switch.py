"""Max Drawdown Kill Switch pure-logic tests (Build Spec §8)."""

import pytest

from src.engine.risk.kill_switch import (
    DEFAULT_MAX_DRAWDOWN_PCT,
    KillSwitchTrippedError,
    evaluate_drawdown,
)


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


# Build Spec §21-22 hardening pass: mutation testing (58% kill rate before
# these) found real gaps below, all confirmed via a real mutmut run against
# this file, not hypothesized.


def test_default_threshold_is_actually_used_when_not_overridden():
    """Every existing test above passes threshold_pct explicitly -- nothing
    exercised DEFAULT_MAX_DRAWDOWN_PCT itself, so a real mutmut run
    silently changed it (15.0 -> 16.0, 15.0 -> None) with the full test
    suite still green either way."""
    assert DEFAULT_MAX_DRAWDOWN_PCT == 15.0
    result = evaluate_drawdown(current_equity=84_000, peak_equity=100_000)
    assert result.should_trip is True  # 16% drawdown, over the 15% default
    result = evaluate_drawdown(current_equity=86_000, peak_equity=100_000)
    assert result.should_trip is False  # 14% drawdown, under the 15% default


def test_peak_equity_of_exactly_one_still_computes_a_real_drawdown():
    """The "no meaningful peak" guard is peak_equity <= 0, not <= 1 -- a
    real mutmut run changed the boundary to <= 1 and nothing caught it,
    since every existing test used peak_equity either 0 or >= 100_000."""
    result = evaluate_drawdown(current_equity=0, peak_equity=1, threshold_pct=15.0)
    assert result.drawdown_pct == 100.0
    assert result.should_trip is True


def test_tripped_error_message_reports_the_actual_reason_when_given():
    """A real mutmut run flipped `reason or 'no reason recorded'` to
    `reason and 'no reason recorded'` -- the exact inverse (shows the
    generic fallback when a real reason WAS given, shows nothing when it
    wasn't) -- and every existing test passed anyway, because nothing
    asserted on this exception's message content at all. This message is
    what a human operator reads to understand why trading stopped."""
    exc = KillSwitchTrippedError("live", "drawdown exceeded 15.0%")
    assert exc.mode == "live"
    assert exc.reason == "drawdown exceeded 15.0%"
    assert str(exc) == "live kill switch is tripped: drawdown exceeded 15.0%"


def test_tripped_error_message_falls_back_when_no_reason_given():
    exc = KillSwitchTrippedError("paper", None)
    assert exc.reason is None
    assert str(exc) == "paper kill switch is tripped: no reason recorded"


def test_drawdown_evaluation_is_actually_frozen():
    """DrawdownEvaluation's docstring is explicit that it never fabricates
    a result -- frozen=True is what makes that a property callers can
    trust rather than something that merely happens not to be mutated
    anywhere today. A real mutmut run flipped it to frozen=False and every
    existing test passed anyway, since none of them ever tried to mutate
    a result."""
    from dataclasses import FrozenInstanceError

    result = evaluate_drawdown(current_equity=90_000, peak_equity=100_000, threshold_pct=15.0)
    with pytest.raises(FrozenInstanceError):
        result.should_trip = True
