"""Keep a test from running across midnight IST.

Some tests read "today in IST" and then let the code under test read it again (a fill stamped now,
then a review of today; an enrolment judged against a "tomorrow" the test computed). If midnight IST
falls between the two reads, they name different days and the test fails for no reason in the code.
Rather than find and freeze every such pair, `conftest.py` waits out the last `MARGIN_SECONDS`
before midnight IST before each test starts, so a test of up to that length cannot straddle it.
The cost is one wait of at most a minute, on the one run in a day that reaches that minute.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARGIN_SECONDS = 60.0


def seconds_to_wait(now: datetime, margin_seconds: float = MARGIN_SECONDS) -> float:
    """How long to wait before starting a test at `now` (timezone-aware) so that midnight IST is not
    within the next `margin_seconds`: zero when it is further away, otherwise until one second past
    it."""
    if now.tzinfo is None:
        raise ValueError("seconds_to_wait needs a timezone-aware instant")
    local = now.astimezone(IST)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), IST)
    until = (next_midnight - local).total_seconds()
    return until + 1.0 if until <= margin_seconds else 0.0


def now_utc() -> datetime:
    return datetime.now(UTC)
