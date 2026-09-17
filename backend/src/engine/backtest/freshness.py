"""Data-freshness gate (Build Spec §10): refuses to backtest if the data
lake lacks the prior trading day's data.

**Phase 10 wiring**: `previous_trading_day` now delegates to the real NSE
holiday calendar (`src.data.nse_calendar.previous_nse_trading_day`) instead
of the old weekend-only approximation -- the Phase 5 gap this module's
docstring used to document by name is closed. The gate itself
(`check_data_freshness`) is unchanged: it still takes any
`DataLakeFreshnessCheck` implementation, protocol-swap style. What changed
is which implementation production code actually constructs --
`src.api.routes.backtests` now builds a real
`src.data.freshness_snapshot.DataLakeFreshnessSnapshot` from the ingested
data lake instead of `FakeDataLakeFreshness`. `FakeDataLakeFreshness`
itself stays right here, unchanged -- it remains a legitimate, fast,
dependency-free fixture for tests, exactly like every other Fake-prefixed
stand-in elsewhere in this codebase (e.g. Phase 6's
`FakeNiftyBenchmarkProvider`); only *production* code stopped constructing
one.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from src.data.nse_calendar import previous_nse_trading_day


def previous_trading_day(as_of: date) -> date:
    return previous_nse_trading_day(as_of)


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
