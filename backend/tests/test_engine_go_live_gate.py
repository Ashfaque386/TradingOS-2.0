"""Go-Live Readiness Gate tests (Build Spec §8): ALL four conditions are
required -- each one failing alone must block eligibility, independent of
the other three passing.
"""

from src.engine.risk.go_live_gate import GoLiveReadinessInput, evaluate_go_live_readiness

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
