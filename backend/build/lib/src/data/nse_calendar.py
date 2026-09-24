"""NSE trading-holiday calendar (Build Spec §14): replaces the weekend-only
approximation that `src.engine.backtest.freshness.previous_trading_day` and
`src.engine.paper_trading.market_hours` both explicitly documented as a
"Phase 10 data-ingestion concern" -- this is that real lookup.

**Two tiers, on purpose, never fabricated**: NSE's annual holiday list is
a mix of **fixed-date** national holidays (Republic Day, Maharashtra Day,
Independence Day, Gandhi Jayanti, Christmas -- same calendar date every
year, generated programmatically below for a fixed year range rather than
hand-transcribed) and **movable, lunisolar-calendar** holidays (Holi, Ram
Navami, Eid, Ganesh Chaturthi, Dussehra, Diwali, Guru Nanak Jayanti, Good
Friday, ...) whose exact date shifts every year and is only known once NSE
publishes that year's dated circular. Hardcoding invented movable-holiday
dates for a year this build has no verified circular for would silently
fabricate regulatory data -- the same class of dishonesty this codebase
refuses everywhere else (see e.g. `src.engine.backtest.comparison`'s
null-correlation rule, or every other "honest stub" in this build).
Instead, movable holidays are loaded from a plain JSON file
(`settings.nse_holidays_path`, default `config/nse_holidays.json`) that an
operator maintains against NSE's real published circular; this repo ships
that file seeded with real 2025 dates as a working example. A missing or
unlisted year simply falls back to the fixed-date holidays alone -- an
honest, documented gap, not a crash.
"""

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from src.core.config import get_settings

_FIXED_HOLIDAY_MONTH_DAY: tuple[tuple[int, int], ...] = (
    (1, 26),  # Republic Day
    (5, 1),  # Maharashtra Day
    (8, 15),  # Independence Day
    (10, 2),  # Gandhi Jayanti
    (12, 25),  # Christmas
)

# Generated, not hand-transcribed, for a fixed year span comfortably
# covering this build's dev/test/demo usage.
_FIXED_HOLIDAY_YEARS = range(2020, 2031)


@lru_cache(maxsize=1)
def _fixed_holidays() -> frozenset[date]:
    return frozenset(
        date(year, month, day)
        for year in _FIXED_HOLIDAY_YEARS
        for month, day in _FIXED_HOLIDAY_MONTH_DAY
    )


def load_extra_holidays(path: str | Path | None = None) -> frozenset[date]:
    """Reads the movable-holiday JSON reference file. Returns an empty set
    (not an error) if the file is absent -- same "protocol swap, missing is
    fine" posture as every other optional data source in this codebase."""
    settings = get_settings()
    target = Path(path) if path is not None else Path(settings.nse_holidays_path)
    if not target.exists():
        return frozenset()
    payload = json.loads(target.read_text(encoding="utf-8"))
    return frozenset(date.fromisoformat(d) for d in payload.get("holidays", []))


def is_nse_trading_day(day: date, *, extra_holidays: frozenset[date] | None = None) -> bool:
    if day.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    holidays = extra_holidays if extra_holidays is not None else load_extra_holidays()
    return day not in _fixed_holidays() and day not in holidays


def previous_nse_trading_day(as_of: date, *, extra_holidays: frozenset[date] | None = None) -> date:
    holidays = extra_holidays if extra_holidays is not None else load_extra_holidays()
    day = as_of - timedelta(days=1)
    while not is_nse_trading_day(day, extra_holidays=holidays):
        day -= timedelta(days=1)
    return day
