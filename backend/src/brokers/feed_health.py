"""What the broker actually said about the stored credentials.

`BrokerCredentials.token_status` is derived from the clock alone -- the daily
"expires around 06:00 IST" estimate -- so Settings kept showing "connected" while
Zerodha was answering every quote with a 403. This holds the one fact the clock
cannot know: the quote feed was refused, when, and with what.

Written by `BrokerQuoteTickSource` the moment a broker rejects the credentials and
cleared when a call succeeds again, when the credentials are saved, deleted or
re-authenticated, and when the tick source is rebuilt for new credentials. Read by
`GET /api/v1/broker-credentials`. Process-local on purpose: it is a live signal that
the next market-hours firing re-establishes, not a record, and a restart showing
nothing is honest ("not known yet") rather than stale.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class FeedRejection:
    status_code: int
    detail: str
    at: datetime


@dataclass(frozen=True, slots=True)
class QuoteRefusals:
    """Quotes a broker answered that were not acted on (their exchange stamp was from a previous
    day, or too old): how many since the credentials last changed, and the latest."""

    count: int
    last_symbol: str
    last_reason: str
    last_at: datetime


_rejections: dict[str, FeedRejection] = {}
_refusals: dict[str, QuoteRefusals] = {}


def record_rejection(broker: str, *, status_code: int, detail: str) -> None:
    _rejections[broker] = FeedRejection(status_code, detail[:200], datetime.now(UTC))


def clear(broker: str | None = None) -> None:
    """Forget the rejection for one broker, or every broker."""
    if broker is None:
        _rejections.clear()
        _refusals.clear()
    else:
        _rejections.pop(broker, None)
        _refusals.pop(broker, None)


def get_rejection(broker: str) -> FeedRejection | None:
    return _rejections.get(broker)


def record_quote_refusal(broker: str, *, symbol: str, reason: str) -> None:
    previous = _refusals.get(broker)
    now = datetime.now(UTC)
    if previous is None:
        _refusals[broker] = QuoteRefusals(1, symbol, reason[:200], now)
    else:
        _refusals[broker] = replace(
            previous,
            count=previous.count + 1,
            last_symbol=symbol,
            last_reason=reason[:200],
            last_at=now,
        )


def get_quote_refusals(broker: str) -> QuoteRefusals | None:
    return _refusals.get(broker)


def all_rejections() -> dict[str, FeedRejection]:
    """Every broker whose credentials are being refused right now (a copy)."""
    return dict(_rejections)
