"""How far behind the data lake's newest daily data is, in trading days.

The Data Lake Status view says whether each pipeline last ran; this says whether the data a
backtest needs is there. A backtest requires daily data for the last trading day before today
(`src.engine.backtest.freshness`), so the lake is current when its newest daily date has reached
that day, and each trading day short of it is one more day a backtest would be refused for.
"""

from datetime import date, timedelta

from src.data.nse_calendar import is_nse_trading_day


def trading_days_behind(
    newest: date | None, required: date, *, extra_holidays: frozenset[date] | None = None
) -> int | None:
    """Trading days after `newest`, up to and including `required`: 0 when the lake has reached
    `required` (or is ahead of it), None when it holds no daily data at all."""
    if newest is None:
        return None
    behind = 0
    day = newest + timedelta(days=1)
    while day <= required:
        if is_nse_trading_day(day, extra_holidays=extra_holidays):
            behind += 1
        day += timedelta(days=1)
    return behind
