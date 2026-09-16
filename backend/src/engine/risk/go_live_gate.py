"""Go-Live Readiness Gate (Build Spec §8): a strategy may advance out of
Shadow/Paper Mode into live trading only when ALL four conditions hold --
this is a conjunction, never a weighted score or majority vote. Each
condition is evaluated and reported independently so a caller (and a test)
can see exactly which one(s) are failing, not just an opaque overall
verdict.

Win-rate divergence is the one condition that can be *undetermined* rather
than merely failing a threshold (a strategy with no live win rate yet, for
instance) -- an undetermined divergence never fabricates a pass; it is
reported as not-yet-eligible with a reason, the same "never fabricate a
metric" discipline as Phase 5's backtest engine.
"""

from dataclasses import dataclass, field

DEFAULT_MIN_TRADES = 30
DEFAULT_MIN_CALENDAR_DAYS = 21
DEFAULT_MIN_CLEAN_SHADOW_DAYS = 10
DEFAULT_MAX_WIN_RATE_DIVERGENCE_PP = 20.0


@dataclass(frozen=True, slots=True)
class GoLiveReadinessInput:
    num_trades: int
    calendar_days_running: int
    clean_shadow_mode_streak_days: int
    live_win_rate: float | None  # fraction 0..1
    backtest_win_rate: float | None  # fraction 0..1


@dataclass(frozen=True, slots=True)
class GoLiveReadinessResult:
    eligible: bool
    checks: dict[str, bool]
    reasons: list[str] = field(default_factory=list)


def evaluate_go_live_readiness(
    readiness_input: GoLiveReadinessInput,
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_calendar_days: int = DEFAULT_MIN_CALENDAR_DAYS,
    min_clean_shadow_days: int = DEFAULT_MIN_CLEAN_SHADOW_DAYS,
    max_win_rate_divergence_pp: float = DEFAULT_MAX_WIN_RATE_DIVERGENCE_PP,
) -> GoLiveReadinessResult:
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    checks["min_trades"] = readiness_input.num_trades >= min_trades
    if not checks["min_trades"]:
        reasons.append(
            f"only {readiness_input.num_trades} trades recorded, " f"needs >= {min_trades}"
        )

    checks["min_calendar_days"] = readiness_input.calendar_days_running >= min_calendar_days
    if not checks["min_calendar_days"]:
        reasons.append(
            f"only {readiness_input.calendar_days_running} calendar days running, "
            f"needs >= {min_calendar_days}"
        )

    checks["clean_shadow_streak"] = (
        readiness_input.clean_shadow_mode_streak_days >= min_clean_shadow_days
    )
    if not checks["clean_shadow_streak"]:
        reasons.append(
            f"only {readiness_input.clean_shadow_mode_streak_days} clean Shadow Mode "
            f"days, needs >= {min_clean_shadow_days}"
        )

    if readiness_input.live_win_rate is None or readiness_input.backtest_win_rate is None:
        checks["win_rate_divergence"] = False
        reasons.append("win-rate divergence undetermined: missing live or backtest win rate")
    else:
        divergence_pp = (
            abs(readiness_input.live_win_rate - readiness_input.backtest_win_rate) * 100.0
        )
        checks["win_rate_divergence"] = divergence_pp <= max_win_rate_divergence_pp
        if not checks["win_rate_divergence"]:
            reasons.append(
                f"live/backtest win-rate divergence {divergence_pp:.2f}pp exceeds "
                f"{max_win_rate_divergence_pp:.2f}pp"
            )

    return GoLiveReadinessResult(eligible=all(checks.values()), checks=checks, reasons=reasons)
