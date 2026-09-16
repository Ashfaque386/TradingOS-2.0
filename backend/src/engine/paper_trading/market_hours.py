"""IST-aware NSE market-hours check (Build Spec §11): gates every intraday
(Layer 2) tick-processing pass so it only acts during NSE's regular
equity/derivatives session, 09:15-15:30 IST, Monday-Friday.

**Documented approximation**: this is weekday-aware only, not a full NSE
trading-holiday calendar -- the same honest gap Phase 5's
`src.engine.backtest.freshness.previous_trading_day` documents for the
same reason (a real exchange calendar is a Phase 10 data-ingestion
concern). A job scheduled against this check will incorrectly believe the
market is open on an NSE holiday that falls on a weekday. Replace this
with a real calendar lookup once Phase 10 provides one before treating
this as authoritative for anything beyond development and testing.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)


def is_market_open_ist(now: datetime | None = None) -> bool:
    """`now` may be naive (assumed UTC, matching this codebase's other
    `datetime.now(UTC)` convention) or timezone-aware; always converted to
    IST before the weekday/time-window check."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    now_ist = now.astimezone(IST)
    if now_ist.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False

    return MARKET_OPEN_IST <= now_ist.time() <= MARKET_CLOSE_IST
