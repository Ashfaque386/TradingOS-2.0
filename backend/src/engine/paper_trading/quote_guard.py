"""Is the exchange's own stamp on a quote too old to act on?

A broker answers a quote request whether or not the market is trading. On a day the exchange is
closed (a holiday the calendar does not know about, an unscheduled closure), or for an instrument
that has not traded, it returns the last price it has -- stamped with the exchange's time for that
price, which is from an earlier day. Without this check that price is published as if it were live,
and the engine would enter, hold and stop out on it.

The rule is deliberately narrow, because a quote's stamp is the time of the last *trade*, and a
quiet instrument legitimately has an old one:

- a stamp from an earlier **IST date** is refused: nothing traded today, whatever the clock says;
- optionally (`PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS`, off by default until a session's measured lag
  says what is normal) a stamp older than that many seconds is refused too;
- a quote with **no stamp** (Upstox's is not yet verified) or one **in the future** (clock skew) is
  let through: the check can only refuse what it can read, and says nothing about what it cannot.

Pure: no clock, network or settings of its own.
"""

from datetime import datetime

from src.engine.paper_trading.market_hours import IST

# A quote stamped slightly ahead of this machine's clock is clock skew, not a future quote.
EXCHANGE_CLOCK_SKEW_SECONDS = 2.0


def exchange_stamp_problem(
    exchange_time: datetime | None, *, now: datetime, max_age_seconds: float = 0.0
) -> str | None:
    """Why a quote with this exchange stamp must not be acted on, or `None` if it may be (including
    when there is no stamp to judge). `now` and `exchange_time` must be timezone-aware."""
    if exchange_time is None:
        return None
    age = (now - exchange_time).total_seconds()
    if age < -EXCHANGE_CLOCK_SKEW_SECONDS:
        return None
    stamped = exchange_time.astimezone(IST)
    if stamped.date() < now.astimezone(IST).date():
        return f"the exchange stamp ({stamped:%Y-%m-%d %H:%M} IST) is from a previous day"
    if max_age_seconds > 0 and age > max_age_seconds:
        return f"the exchange stamp is {age:.0f}s old (the limit is {max_age_seconds:.0f}s)"
    return None
