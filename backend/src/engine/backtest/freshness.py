"""Data-freshness gate (Build Spec §10): refuses to backtest if the data
lake lacks the prior trading day's data.

The real data lake (`src/data/`) doesn't ship until Phase 10 -- this gate
is written against the `DataLakeFreshnessCheck` protocol below, and
`FakeDataLakeFreshness` is a deterministic, dependency-free stand-in for
it (same honest-stub posture as every other not-yet-built external
integration in this codebase). Swapping in the real Phase 10 lookup later
is a one-line change at each call site (pass a different implementation
of the protocol), not a rewrite of this gate's logic.

"Prior trading day" here means the most recent Monday-Friday date before
`as_of`, skipping weekends only -- **not** a full NSE holiday calendar,
which is itself a Phase 10 data concern. This is a documented
approximation: a backtest requested the day after an NSE holiday will
incorrectly demand data for that holiday. Replace `previous_trading_day`
with a real exchange calendar lookup once Phase 10 provides one before
treating this gate as authoritative for anything beyond development and
testing.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol


def previous_trading_day(as_of: date) -> date:
    day = as_of - timedelta(days=1)
    while day.weekday() >= 5:  # 5=Saturday, 6=Sunday
        day -= timedelta(days=1)
    return day


class DataLakeFreshnessCheck(Protocol):
    def has_data_for(self, symbol: str, day: date) -> bool: ...


@dataclass(frozen=True, slots=True)
class FakeDataLakeFreshness:
    """`available_dates` maps symbol -> the set of dates it actually has
    data for -- a test/dev fixture seeds this directly rather than
    reading any real storage.
    """

    available_dates: dict[str, frozenset[date]]

    def has_data_for(self, symbol: str, day: date) -> bool:
        return day in self.available_dates.get(symbol, frozenset())


@dataclass(frozen=True, slots=True)
class FreshnessCheckResult:
    fresh: bool
    required_date: date
    reason: str | None = None


def check_data_freshness(
    *, symbol: str, as_of: date, data_lake: DataLakeFreshnessCheck
) -> FreshnessCheckResult:
    required = previous_trading_day(as_of)
    if data_lake.has_data_for(symbol, required):
        return FreshnessCheckResult(fresh=True, required_date=required)
    return FreshnessCheckResult(
        fresh=False,
        required_date=required,
        reason=(
            f"data lake lacks {symbol} data for {required.isoformat()} "
            f"(the prior trading day as of {as_of.isoformat()})"
        ),
    )
