"""The broker's live market depth, as paper fills use it (real-market-data plan, phase 3).

The tick job asks the broker for quotes in one batched call; each quote carries the 5-level book.
The last price goes onto the tick stream as before, and the book is stored beside it as the
symbol's *latest* depth snapshot (`paper:depth:{symbol}`, one JSON value that expires on its own) --
not appended to the stream, which would multiply its size by ten for data only the newest copy of
which is of any use.

`RealDepthOrderBookProvider` is the `OrderBookProvider` a paper fill walks when the data is real.
It never guesses: a symbol with no snapshot, a snapshot older than `max_age_seconds`, or a book
with nothing on the side the order needs, is `DepthUnavailableError`, and the caller leaves the
order unfilled (the signal stays pending) instead of walking a made-up book.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from redis.asyncio import Redis

from src.core.config import get_settings
from src.engine.paper_trading.order_book import (
    MockOrderBookProvider,
    OrderBookLevel,
    OrderBookProvider,
    OrderBookSnapshot,
    Side,
)
from src.models.data_source import DataSource


def depth_key(symbol: str) -> str:
    return f"paper:depth:{symbol}"


class DepthUnavailableError(Exception):
    """No usable real order book for this symbol right now (missing, stale or empty on the side
    needed). The order is not filled."""


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    """A symbol's book as of `timestamp_ms`, best level first on each side."""

    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    timestamp_ms: int
    open_interest: float | None = None

    def is_empty(self) -> bool:
        return not self.bids and not self.asks

    def to_json(self) -> str:
        return json.dumps(
            {
                "bids": [[level.price, level.quantity] for level in self.bids],
                "asks": [[level.price, level.quantity] for level in self.asks],
                "timestamp_ms": self.timestamp_ms,
                "oi": self.open_interest,
            }
        )

    @staticmethod
    def from_json(raw: str) -> "DepthSnapshot":
        data = json.loads(raw)
        return DepthSnapshot(
            bids=tuple(OrderBookLevel(price=float(p), quantity=int(q)) for p, q in data["bids"]),
            asks=tuple(OrderBookLevel(price=float(p), quantity=int(q)) for p, q in data["asks"]),
            timestamp_ms=int(data["timestamp_ms"]),
            open_interest=data.get("oi"),
        )


async def publish_depth(
    redis: Redis, symbol: str, snapshot: DepthSnapshot, *, ttl_seconds: float
) -> None:
    """Stores `snapshot` as the symbol's latest book. It expires after `ttl_seconds` so a feed
    that has stopped leaves no book behind for a fill to find."""
    await redis.set(depth_key(symbol), snapshot.to_json(), px=max(1, int(ttl_seconds * 1000)))


async def load_depth(redis: Redis, symbol: str) -> DepthSnapshot | None:
    raw = await redis.get(depth_key(symbol))
    if raw is None:
        return None
    return DepthSnapshot.from_json(raw if isinstance(raw, str) else raw.decode("utf-8"))


@dataclass
class RealDepthOrderBookProvider:
    """Order books from the broker's live depth, via Redis. `snapshot_async` is what the
    paper-trading orchestration awaits (see `book_for`); the synchronous `snapshot` of the
    `OrderBookProvider` protocol cannot read Redis and refuses, so a caller that forgot to await
    fails loudly instead of filling on nothing."""

    redis: Redis
    max_age_seconds: float = 30.0
    clock: Callable[[], float] = time.time
    data_source: ClassVar[str] = DataSource.REAL

    def snapshot(self, symbol: str, *, mid_price: float) -> OrderBookSnapshot:
        raise DepthUnavailableError(
            "real depth is read from Redis: use `snapshot_async` (see order_book.book_for)"
        )

    async def snapshot_async(
        self, symbol: str, *, side: Side, mid_price: float
    ) -> OrderBookSnapshot:
        depth = await load_depth(self.redis, symbol)
        if depth is None:
            raise DepthUnavailableError(f"no live order book for {symbol}")
        age = self.clock() - depth.timestamp_ms / 1000.0
        if age > self.max_age_seconds:
            raise DepthUnavailableError(
                f"the order book for {symbol} is {age:.0f}s old (limit {self.max_age_seconds:.0f}s)"
            )
        needed = depth.asks if side == "buy" else depth.bids
        if not needed:
            raise DepthUnavailableError(
                f"the order book for {symbol} has no {'asks' if side == 'buy' else 'bids'}"
            )
        return OrderBookSnapshot(bids=list(depth.bids), asks=list(depth.asks))


def build_order_book_provider(redis: Redis) -> OrderBookProvider:
    """The book paper fills walk: the broker's real depth, or -- only in synthetic demo/test mode
    (`PAPER_TRADING_SYNTHETIC`) -- the generated one."""
    settings = get_settings()
    if settings.paper_trading_synthetic:
        return MockOrderBookProvider()
    return RealDepthOrderBookProvider(redis, max_age_seconds=settings.paper_depth_max_age_seconds)
