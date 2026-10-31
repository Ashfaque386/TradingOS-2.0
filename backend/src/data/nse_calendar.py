"""NSE trading-holiday calendar (Build Spec §14): replaces the weekend-only
approximation that `src.engine.backtest.freshness.previous_trading_day` and
`src.engine.paper_trading.market_hours` both explicitly documented as a
"Phase 10 data-ingestion concern" -- this is that real lookup.

**Two tiers, on purpose, never fabricated**: NSE's annual holiday list is
a mix of **fixed-date** national holidays (Republic Day, Ambedkar Jayanti,
Maharashtra Day, Independence Day, Gandhi Jayanti, Christmas -- same
calendar date every year, generated programmatically below for a fixed
year range rather than hand-transcribed) and **movable, lunisolar-calendar** holidays (Holi, Ram
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
that file is seeded from NSE's published circulars. A missing or unlisted
year simply falls back to the fixed-date holidays alone -- an honest,
documented gap, not a crash -- and `calendar_file_covers_year` says which
years the file has anything for, so the gap can be shown instead of found
by trading on a closed day.

Each entry in the file is either a bare "YYYY-MM-DD" string or an object
{"date": "YYYY-MM-DD", "name": "Holi"}; the name is only for display.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from src.core.config import get_settings

_FIXED_HOLIDAYS: tuple[tuple[int, int, str], ...] = (
    (1, 26, "Republic Day"),
    (4, 14, "Dr. Baba Saheb Ambedkar Jayanti"),
    (5, 1, "Maharashtra Day"),
    (8, 15, "Independence Day"),
    (10, 2, "Mahatma Gandhi Jayanti"),
    (12, 25, "Christmas"),
)

# Generated, not hand-transcribed, for a fixed year span: far enough ahead that the calendar does
# not quietly stop knowing about them (it stopped at 2030 until now; the movable holidays are the
# part that needs a yearly update, and `calendar_file_covers_year` shows when that is overdue).
_FIXED_HOLIDAY_YEARS = range(2020, 2041)

SOURCE_FIXED = "built in (fixed date)"
SOURCE_CALENDAR_FILE = "calendar file"


@dataclass(frozen=True, slots=True)
class Holiday:
    day: date
    name: str | None
    source: str

    @property
    def on_weekend(self) -> bool:
        return self.day.weekday() >= 5


@lru_cache(maxsize=1)
def _fixed_holiday_names() -> dict[date, str]:
    return {
        date(year, month, day): name
        for year in _FIXED_HOLIDAY_YEARS
        for month, day, name in _FIXED_HOLIDAYS
    }


@lru_cache(maxsize=1)
def _fixed_holidays() -> frozenset[date]:
    return frozenset(_fixed_holiday_names())


def load_extra_holiday_entries(path: str | Path | None = None) -> dict[date, str | None]:
    """Reads the movable-holiday JSON reference file as {date: name or None}. Returns an empty
    mapping (not an error) if the file is absent -- same "protocol swap, missing is fine" posture
    as every other optional data source in this codebase."""
    settings = get_settings()
    target = Path(path) if path is not None else Path(settings.nse_holidays_path)
    if not target.exists():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    entries: dict[date, str | None] = {}
    for item in payload.get("holidays", []):
        if isinstance(item, str):
            entries[date.fromisoformat(item)] = None
        else:
            entries[date.fromisoformat(item["date"])] = item.get("name")
    return entries


def load_extra_holidays(path: str | Path | None = None) -> frozenset[date]:
    """The dates in the movable-holiday file (see `load_extra_holiday_entries`)."""
    return frozenset(load_extra_holiday_entries(path))


def calendar_file_covers_year(year: int, *, path: str | Path | None = None) -> bool:
    """Whether the movable-holiday file lists anything for `year`. False means every movable
    holiday of that year (Holi, Eid, Diwali ...) is currently treated as a trading day."""
    return any(day.year == year for day in load_extra_holiday_entries(path))


def holidays_for_year(year: int, *, path: str | Path | None = None) -> list[Holiday]:
    """Every holiday the calendar knows for `year`, weekends included (flagged), by date."""
    holidays: dict[date, Holiday] = {}
    for day, name in load_extra_holiday_entries(path).items():
        if day.year == year:
            holidays[day] = Holiday(day, name, SOURCE_CALENDAR_FILE)
    for day, name in _fixed_holiday_names().items():
        if day.year == year:
            holidays[day] = Holiday(day, name, SOURCE_FIXED)
    return sorted(holidays.values(), key=lambda holiday: holiday.day)


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
