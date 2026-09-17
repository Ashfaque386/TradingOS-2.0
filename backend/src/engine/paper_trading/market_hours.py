"""IST-aware NSE market-hours check (Build Spec §11): gates every intraday
(Layer 2) tick-processing pass so it only acts during NSE's regular
equity/derivatives session, 09:15-15:30 IST, Monday-Friday, and -- as of
Phase 10 -- excluding real NSE trading holidays.

**Phase 10 wiring**: this used to be weekday-aware only, the same honest
gap `src.engine.backtest.freshness.previous_trading_day` used to document
for the same reason (a real exchange calendar was a Phase 10 concern).
Both now consult `src.data.nse_calendar`, which is itself an honestly
documented partial calendar (fixed-date national holidays are generated in
code; movable/lunisolar holidays come from an operator-maintained JSON
reference file that may not cover every year) -- see that module's
docstring for why exact movable-holiday dates are never fabricated in
code.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from src.data.nse_calendar import is_nse_trading_day

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)


def is_market_open_ist(now: datetime | None = None) -> bool:
    """`now` may be naive (assumed UTC, matching this codebase's other
    `datetime.now(UTC)` convention) or timezone-aware; always converted to
    IST before the trading-day/time-window check."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    now_ist = now.astimezone(IST)
    if not is_nse_trading_day(now_ist.date()):
        return False

    return MARKET_OPEN_IST <= now_ist.time() <= MARKET_CLOSE_IST
