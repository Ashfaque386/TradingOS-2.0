"""The evidence the Go-Live Readiness Gate is measured on, worked out from what the system recorded
(Phase 4), instead of typed in by the person signing off.

The gate (src.engine.risk.go_live_gate) has four conditions. Each one's input is derived here:

* **Trades** -- closing fills (a fill that realized a profit or a loss) by the strategy's paper
  subscriptions. Only fills that rested on real prices count: `real`, or `mixed` (a real tick walked
  through the simulated order book, which is what a paper fill on a broker quote is). Fills on
  generated prices, or on an operator-typed price, and hand-made administrative closes, are left out
  and counted separately, so the reader can see what was set aside.
* **Calendar days** -- from the first counted fill to today (IST).
* **Win rate** -- the share of counted closing fills whose profit, net of that fill's costs, is
  above zero, against the win rate of the strategy's latest completed backtest *on real data*. A
  backtest on bars a caller posted or on generated bars is not evidence and is not used; with none,
  the divergence is undetermined and the gate does not pass it.
* **Clean Shadow Mode streak** -- consecutive NSE trading days, ending at the latest one, on each of
  which at least one dry run was made for this strategy and none failed. A day nobody tried is not a
  clean day; a trading day with no run ends the streak (weekends and holidays are skipped, and
  today, which may not have been tried yet, does not break it).

Nothing here is a cached pass: it is recomputed from the records on every call.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.nse_calendar import is_nse_trading_day
from src.engine.paper_trading.market_hours import IST
from src.engine.risk.go_live_gate import (
    GoLiveReadinessInput,
    GoLiveReadinessResult,
    evaluate_go_live_readiness,
)
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.data_source import DataSource
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeOutcome, ShadowModeRun
from src.models.strategy_version import StrategyVersion

__all__ = [
    "COUNTED_DATA_SOURCES",
    "GoLiveEvidence",
    "NotDerived",
    "clean_shadow_streak",
    "derive_go_live_evidence",
]

# A paper fill's data_source combines its tick's source with the order book's, and the book is
# always simulated: so a fill on a real broker quote is `mixed`, and `real` would be a fill on a
# real book.
COUNTED_DATA_SOURCES = (DataSource.REAL, DataSource.MIXED)
_BACKTEST_DATA_SOURCES = (DataSource.REAL, DataSource.MIXED)

_STREAK_LOOKBACK_DAYS = 120


@dataclass(frozen=True, slots=True)
class NotDerived:
    """An input the records cannot support, and why (the gate treats it as not met)."""

    input: str
    reason: str


@dataclass(frozen=True, slots=True)
class GoLiveEvidence:
    strategy_id: uuid.UUID
    as_of: datetime
    inputs: GoLiveReadinessInput
    # What was counted and what was left out, so the numbers can be checked against the fills.
    counted_closing_fills: int
    counted_fills: int
    excluded_unreal_fills: int
    excluded_administrative_fills: int
    first_counted_fill_at: datetime | None
    backtest_run_id: uuid.UUID | None
    backtest_data_source: str | None
    shadow_clean_days_real_sandbox: int
    shadow_runs_failed_recently: int
    not_derived: list[NotDerived] = field(default_factory=list)

    def readiness(self) -> GoLiveReadinessResult:
        return evaluate_go_live_readiness(self.inputs)


def clean_shadow_streak(
    worked_days: set[date],
    failed_days: set[date],
    today: date,
    *,
    is_trading_day: Callable[[date], bool] = is_nse_trading_day,
) -> int:
    """Consecutive trading days of clean dry runs, ending at the latest one.

    `worked_days` are the IST dates with at least one dry run that worked, `failed_days` those with
    at least one that failed. A day is clean when it is in the first and not in the second. Today
    is not held against the strategy if nothing has been tried yet today, but a failed run today
    is: the streak is then 0.
    """
    streak = 0
    day = today
    if is_trading_day(day):
        if day in failed_days:
            return 0
        if day in worked_days:
            streak = 1
        # neither: today simply has not been tried yet
        day -= timedelta(days=1)
    for _ in range(_STREAK_LOOKBACK_DAYS):
        if not is_trading_day(day):
            day -= timedelta(days=1)
            continue
        if day in failed_days or day not in worked_days:
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


def _ist_date(moment: datetime) -> date:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(IST).date()


async def derive_go_live_evidence(
    db: AsyncSession, strategy_id: uuid.UUID, *, now: datetime | None = None
) -> GoLiveEvidence:
    now = now or datetime.now(UTC)
    today = _ist_date(now)
    not_derived: list[NotDerived] = []

    fills = (
        (
            await db.execute(
                select(PaperFill)
                .join(
                    PaperTradingSubscription,
                    PaperFill.subscription_id == PaperTradingSubscription.id,
                )
                .join(
                    StrategyVersion,
                    PaperTradingSubscription.strategy_version_id == StrategyVersion.id,
                )
                .where(StrategyVersion.strategy_id == strategy_id, PaperFill.filled_quantity > 0)
                .order_by(PaperFill.created_at)
            )
        )
        .scalars()
        .all()
    )
    administrative = [f for f in fills if f.administrative]
    unreal = [
        f for f in fills if not f.administrative and f.data_source not in COUNTED_DATA_SOURCES
    ]
    counted = [f for f in fills if not f.administrative and f.data_source in COUNTED_DATA_SOURCES]
    closing = [f for f in counted if f.realized_pnl != 0.0]
    wins = [f for f in closing if f.realized_pnl - f.costs_total > 0]

    first_at = counted[0].created_at if counted else None
    calendar_days = max(0, (today - _ist_date(first_at)).days) if first_at else 0
    live_win_rate = (len(wins) / len(closing)) if closing else None

    if not counted:
        not_derived.append(
            NotDerived(
                "num_trades",
                "no paper fill on real prices has been recorded for this strategy yet"
                + (
                    f" ({len(unreal)} on generated or typed prices were set aside)"
                    if unreal
                    else ""
                ),
            )
        )
    if live_win_rate is None:
        not_derived.append(
            NotDerived(
                "live_win_rate",
                "no closing fill on real prices yet, so there is no win rate to compare",
            )
        )

    backtest = (
        (
            await db.execute(
                select(BacktestRun)
                .join(StrategyVersion, BacktestRun.strategy_version_id == StrategyVersion.id)
                .where(
                    StrategyVersion.strategy_id == strategy_id,
                    BacktestRun.status == BacktestStatus.COMPLETED,
                )
                .order_by(BacktestRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    usable = next(
        (
            b
            for b in backtest
            if b.data_source in _BACKTEST_DATA_SOURCES
            and b.metrics is not None
            and b.metrics.get("win_rate") is not None
        ),
        None,
    )
    backtest_win_rate = float(usable.metrics["win_rate"]) if usable and usable.metrics else None
    if usable is None:
        if not backtest:
            why = "this strategy has no completed backtest"
        else:
            sources = sorted({b.data_source for b in backtest})
            why = (
                "its completed backtests rest on bars this system cannot vouch for "
                f"({', '.join(sources)}); a backtest on real data is needed"
            )
        not_derived.append(NotDerived("backtest_win_rate", why))

    shadow_rows = (
        (
            await db.execute(
                select(ShadowModeRun)
                .where(ShadowModeRun.strategy_id == strategy_id)
                .order_by(ShadowModeRun.created_at)
            )
        )
        .scalars()
        .all()
    )
    worked_days = {
        _ist_date(r.created_at) for r in shadow_rows if r.outcome == ShadowModeOutcome.OK
    }
    failed_days = {
        _ist_date(r.created_at) for r in shadow_rows if r.outcome != ShadowModeOutcome.OK
    }
    streak = clean_shadow_streak(worked_days, failed_days, today)
    if not shadow_rows:
        not_derived.append(
            NotDerived(
                "clean_shadow_mode_streak_days",
                "no Shadow Mode dry run has been made for this strategy: run one, or connect a "
                "broker so the daily dry run can",
            )
        )
    sandbox_days = {
        _ist_date(r.created_at)
        for r in shadow_rows
        if r.outcome == ShadowModeOutcome.OK and r.has_sandbox
    }
    recent_cutoff = now - timedelta(days=14)
    failed_recently = sum(
        1
        for r in shadow_rows
        if r.outcome != ShadowModeOutcome.OK and r.created_at >= recent_cutoff
    )

    return GoLiveEvidence(
        strategy_id=strategy_id,
        as_of=now,
        inputs=GoLiveReadinessInput(
            num_trades=len(closing),
            calendar_days_running=calendar_days,
            clean_shadow_mode_streak_days=streak,
            live_win_rate=live_win_rate,
            backtest_win_rate=backtest_win_rate,
        ),
        counted_closing_fills=len(closing),
        counted_fills=len(counted),
        excluded_unreal_fills=len(unreal),
        excluded_administrative_fills=len(administrative),
        first_counted_fill_at=first_at,
        backtest_run_id=usable.id if usable else None,
        backtest_data_source=usable.data_source if usable else None,
        shadow_clean_days_real_sandbox=len(sandbox_days),
        shadow_runs_failed_recently=failed_recently,
        not_derived=not_derived,
    )
