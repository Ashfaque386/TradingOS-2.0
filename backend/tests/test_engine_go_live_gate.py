"""Go-Live Readiness Gate tests (Build Spec §8): ALL four conditions are
required -- each one failing alone must block eligibility, independent of
the other three passing.
"""

from dataclasses import FrozenInstanceError

import pytest

from src.engine.risk.go_live_gate import (
    GoLiveReadinessInput,
    GoLiveReadinessResult,
    evaluate_go_live_readiness,
)

_ALL_GOOD = dict(
    num_trades=30,
    calendar_days_running=21,
    clean_shadow_mode_streak_days=10,
    live_win_rate=0.55,
    backtest_win_rate=0.50,
)


def test_eligible_when_all_four_conditions_hold():
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**_ALL_GOOD))
    assert result.eligible is True
    assert all(result.checks.values())
    assert result.reasons == []


def test_insufficient_trades_alone_blocks_eligibility():
    inputs = {**_ALL_GOOD, "num_trades": 29}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs))
    assert result.eligible is False
    assert result.checks["min_trades"] is False
    assert result.checks["min_calendar_days"] is True
    assert result.checks["clean_shadow_streak"] is True
    assert result.checks["win_rate_divergence"] is True


def test_insufficient_calendar_days_alone_blocks_eligibility():
    inputs = {**_ALL_GOOD, "calendar_days_running": 20}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs))
    assert result.eligible is False
    assert result.checks["min_calendar_days"] is False
    assert result.checks["min_trades"] is True


def test_insufficient_clean_shadow_streak_alone_blocks_eligibility():
    inputs = {**_ALL_GOOD, "clean_shadow_mode_streak_days": 9}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs))
    assert result.eligible is False
    assert result.checks["clean_shadow_streak"] is False
    assert result.checks["min_trades"] is True


def test_excessive_win_rate_divergence_alone_blocks_eligibility():
    inputs = {**_ALL_GOOD, "live_win_rate": 0.90}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs))
    assert result.eligible is False
    assert result.checks["win_rate_divergence"] is False
    assert result.checks["min_trades"] is True


def test_missing_win_rate_never_fabricates_a_pass():
    inputs = {**_ALL_GOOD, "live_win_rate": None}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs))
    assert result.eligible is False
    assert result.checks["win_rate_divergence"] is False


def test_thresholds_are_configurable():
    inputs = {**_ALL_GOOD, "num_trades": 15}
    result = evaluate_go_live_readiness(GoLiveReadinessInput(**inputs), min_trades=10)
    assert result.checks["min_trades"] is True
    assert result.eligible is True


# ---- Found by mutation testing (2026-10-13) -------------------------------------
# mutmut moved the default win-rate limit from 20 to 21 points, scaled the
# divergence by 101 instead of 100, turned `<=` into `<` at the limit, and
# rewrote every reason an operator is shown for being blocked; no test noticed.


def _evaluate(**overrides):
    return evaluate_go_live_readiness(GoLiveReadinessInput(**{**_ALL_GOOD, **overrides}))


def test_the_default_win_rate_limit_is_twenty_points():
    # 20.5 points apart: over the default, so blocked (a limit of 21 would pass it).
    assert _evaluate(live_win_rate=0.705).checks["win_rate_divergence"] is False
    # 19.95 points apart: under it, so allowed (scaling by 101 would block it).
    assert _evaluate(live_win_rate=0.6995).checks["win_rate_divergence"] is True


def test_a_divergence_exactly_at_the_limit_is_allowed():
    # 0.75 - 0.5 is exactly representable, so this is exactly 25 points.
    at_limit = evaluate_go_live_readiness(
        GoLiveReadinessInput(**{**_ALL_GOOD, "live_win_rate": 0.75}),
        max_win_rate_divergence_pp=25.0,
    )
    assert at_limit.checks["win_rate_divergence"] is True
    assert at_limit.eligible is True


def test_each_blocking_reason_says_what_is_short_and_by_how_much():
    assert _evaluate(num_trades=29).reasons == ["only 29 trades recorded, needs >= 30"]
    assert _evaluate(calendar_days_running=20).reasons == [
        "only 20 calendar days running, needs >= 21"
    ]
    assert _evaluate(clean_shadow_mode_streak_days=9).reasons == [
        "only 9 clean Shadow Mode days, needs >= 10"
    ]
    assert _evaluate(live_win_rate=0.90).reasons == [
        "live/backtest win-rate divergence 40.00pp exceeds 20.00pp"
    ]
    assert _evaluate(backtest_win_rate=None).reasons == [
        "win-rate divergence undetermined: missing live or backtest win rate"
    ]


def test_every_failing_check_reports_its_own_reason():
    result = _evaluate(
        num_trades=0, calendar_days_running=0, clean_shadow_mode_streak_days=0, live_win_rate=None
    )

    assert result.eligible is False
    assert len(result.reasons) == 4
    assert not any(result.checks.values())


def test_the_readiness_input_and_verdict_cannot_be_altered_after_the_fact():
    result = _evaluate()

    with pytest.raises(FrozenInstanceError):
        result.eligible = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        GoLiveReadinessInput(**_ALL_GOOD).num_trades = 0  # type: ignore[misc]


def test_a_verdict_built_by_hand_starts_with_no_reasons():
    assert GoLiveReadinessResult(eligible=True, checks={}).reasons == []
