"""Which periods an investor report may be asked for.

The scheduler names its own period (the Monday-to-Sunday week just ended), so a report it makes
is always well formed. A person generating one by hand can ask for anything, and until now a
period that ended before it began, one that had not happened yet, or a "weekly" report over a
quarter was written and filed like any other, an empty or mislabelled document in the history
investors read.
"""

from datetime import date

# The longest period each cadence may cover, in days counted inclusively: the scheduler's week is 7,
# and no calendar month is longer than 31.
MAX_PERIOD_DAYS = {"weekly": 7, "monthly": 31}


class ReportPeriodError(ValueError):
    """The period cannot be reported on; the message says why, for the person who asked."""


def check_report_period(period_start: date, period_end: date, cadence: str, *, today: date) -> int:
    """Raises `ReportPeriodError` for a period that cannot be reported; otherwise returns how many
    days it covers. `today` is the date in India, the exchange's calendar."""
    longest = MAX_PERIOD_DAYS.get(cadence)
    if longest is None:
        raise ReportPeriodError(f"The cadence must be weekly or monthly, not {cadence}.")
    if period_end < period_start:
        raise ReportPeriodError(
            f"The period ends ({period_end.isoformat()}) before it starts "
            f"({period_start.isoformat()})."
        )
    if period_end > today:
        raise ReportPeriodError(
            f"The period ends on {period_end.isoformat()}, after today ({today.isoformat()}) in "
            "India: nothing has happened on those days yet."
        )
    days = (period_end - period_start).days + 1
    if days > longest:
        raise ReportPeriodError(
            f"A {cadence} report covers at most {longest} days; "
            f"{period_start.isoformat()} to {period_end.isoformat()} is {days}."
        )
    return days
