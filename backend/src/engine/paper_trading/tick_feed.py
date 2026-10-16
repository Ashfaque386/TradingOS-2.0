"""Layer 2 tick feed (Build Spec §11): ticks flow through a Redis Stream
per symbol (`paper:ticks:{symbol}`), not pub/sub -- a Stream persists
entries and supports resuming from a cursor, so a consumer that's briefly
behind (a slow pass, a restart) doesn't silently miss ticks the way a
pub/sub subscriber would if it wasn't listening at publish time. That
matters here specifically: the universal stop-loss/re-entry check must
see every tick, not "most of them."

`read_new_ticks` is a simple stateless drain-since-cursor read
(`XRANGE ... (last_id`), not a consumer-group `XREADGROUP` -- this phase
has exactly one intraday consumer per symbol, so there's no competing-
consumer fan-out to justify that extra complexity; the caller
(`src.orchestration.paper_trading`) is responsible for persisting the
returned cursor between calls.

`TickSource` is the interface a real broker-quote poller implements
(Phase 8: src.brokers.tick_source.BrokerQuoteTickSource, which polls
`BrokerAdapter.get_quote`); `MockTickSource` is a deterministic,
per-symbol-seeded synthetic random-walk feed used as the fallback when no
broker credentials are configured -- same honest-stub posture as every
other not-yet-built (or not-yet-credentialed) external integration in
this codebase. `next_tick` is `async` specifically so a real
network-polling implementation can await an HTTP call -- MockTickSource's
own body does no I/O and stays `async` only to satisfy the same Protocol
shape.
"""

import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

import numpy as np
import structlog
from redis.asyncio import Redis

from src.models.data_source import DataSource

logger = structlog.get_logger(__name__)


def tick_stream_key(symbol: str) -> str:
    return f"paper:ticks:{symbol}"


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    price: float
    timestamp_ms: int
    # Where the price came from (a `src.models.data_source.DataSource`): the stream carries
    # it, so whatever consumes the tick later can say what it rested on.
    source: str = "unknown"


async def publish_tick(redis: Redis, tick: Tick) -> str:
    # redis-py's own xadd() return type is untyped (Any) regardless of the
    # real ID string it returns.
    return cast(
        str,
        await redis.xadd(
            tick_stream_key(tick.symbol),
            {
                "price": repr(tick.price),
                "timestamp_ms": str(tick.timestamp_ms),
                "source": tick.source,
            },
        ),
    )


async def read_new_ticks(
    redis: Redis, symbol: str, *, last_id: str = "0", count: int = 1000
) -> tuple[list[Tick], str]:
    """Reads entries strictly after `last_id` ("0" reads from the start of
    the stream). Returns the ticks in stream order plus the new cursor to
    pass as `last_id` next call; an empty read returns `last_id`
    unchanged."""
    start = "-" if last_id == "0" else f"({last_id}"
    entries = await redis.xrange(tick_stream_key(symbol), min=start, count=count)

    ticks: list[Tick] = []
    cursor = last_id
    for entry_id, fields in entries:
        ticks.append(
            Tick(
                symbol=symbol,
                price=float(fields["price"]),
                timestamp_ms=int(fields["timestamp_ms"]),
                # Entries written before this field existed carry no source.
                source=fields.get("source", "unknown"),
            )
        )
        cursor = entry_id
    return ticks, cursor


async def get_latest_tick(redis: Redis, symbol: str) -> Tick | None:
    """The single most recent tick for `symbol`, or `None` if nothing has
    ever been published to its stream -- for a read path (e.g. GET
    /api/v1/paper-trading/pnl/unrealized) that needs "the current price
    right now" rather than `read_new_ticks`'s drain-since-cursor shape."""
    entries = await redis.xrevrange(tick_stream_key(symbol), count=1)
    if not entries:
        return None
    _entry_id, fields = entries[0]
    return Tick(
        symbol=symbol,
        price=float(fields["price"]),
        timestamp_ms=int(fields["timestamp_ms"]),
        source=fields.get("source", "unknown"),
    )


class TickSource(Protocol):
    async def next_tick(self, symbol: str) -> Tick: ...


@dataclass
class MockTickSource:
    """Deterministic-ish synthetic random-walk generator, one RNG per
    symbol so repeated runs against the same symbol set behave the same.
    Not a real broker feed -- src.brokers.tick_source.BrokerQuoteTickSource
    implements the same `TickSource` shape against a real endpoint, and is
    what src.brokers.tick_source.build_tick_source() returns instead of
    this class once broker credentials are configured."""

    default_base_price: float = 100.0
    tick_vol: float = 0.0015
    _rng_by_symbol: dict[str, np.random.Generator] = field(default_factory=dict, repr=False)
    _last_price_by_symbol: dict[str, float] = field(default_factory=dict, repr=False)

    async def next_tick(self, symbol: str) -> Tick:
        # zlib.crc32, not Python's builtin hash(): str hashing is salted
        # per-process by default, which would make this "deterministic"
        # feed give a different sequence on every process restart.
        rng = self._rng_by_symbol.setdefault(
            symbol, np.random.default_rng(zlib.crc32(symbol.encode("utf-8")))
        )
        base = self._last_price_by_symbol.setdefault(symbol, self.default_base_price)
        move = rng.normal(0, self.tick_vol)
        new_price = round(max(base * (1 + move), 0.01), 2)
        self._last_price_by_symbol[symbol] = new_price
        return Tick(
            symbol=symbol,
            price=new_price,
            timestamp_ms=int(time.time() * 1000),
            source=DataSource.SYNTHETIC,  # a seeded random walk, not the market
        )


class TickSourceUnavailableError(Exception):
    """Raised by a `TickSource` that cannot produce a tick *and knows it will
    keep failing until something changes* (a broker that rejected the stored
    credentials). Distinct from an ordinary failure so a source can stop
    calling a service that has already said no, instead of retrying on every
    firing of the job that drives it."""


class UnavailableTickSource:
    """The `TickSource` used when there is no real source and synthetic mode is off: every call
    says so. Paper trading does not run on generated prices for real symbols -- the subscription
    waits (the job reports it) until a broker is connected."""

    def __init__(self, reason: str = "no broker is connected") -> None:
        self.reason = reason

    async def next_tick(self, symbol: str) -> Tick:
        raise TickSourceUnavailableError(
            f"{self.reason}; paper trading does not run on synthetic data "
            "(connect a broker in Settings, or set PAPER_TRADING_SYNTHETIC=true for a demo)"
        )


_MAX_SYMBOLS_IN_ERROR = 5
_MAX_REASON_CHARS = 120

SYMBOL_BACKOFF_INITIAL_SECONDS = 30.0
SYMBOL_BACKOFF_MAX_SECONDS = 600.0


class SymbolBackedOffError(Exception):
    """Stands in for a symbol that was not asked about this round because it
    failed recently. Carried in `TickPublishError.failures`, so the job still
    reports the symbol every firing (it is still not getting ticks) without
    calling the broker for it."""

    def __init__(self, symbol: str, retry_in: float, last_error: str):
        self.symbol = symbol
        self.retry_in = retry_in
        super().__init__(f"backing off for {retry_in:.0f}s after: {last_error}")


class SymbolBackoff:
    """Per-symbol back-off for the tick publisher: a symbol that failed is not
    asked about again for `initial_seconds`, then twice that, and so on up to
    `max_seconds`; one success clears it. Without it a symbol the broker cannot
    quote (unknown, expired, delisted) costs one broker call every firing -- 1,200
    an hour at the 3-second cadence -- for as long as its subscription exists.

    Held by whoever owns the job, and tied to the `TickSource` it was learned
    against: a new source (the broker credentials changed, so
    `get_tick_source` built a fresh one) clears everything, because the cause
    may be gone.
    """

    def __init__(
        self,
        *,
        initial_seconds: float = SYMBOL_BACKOFF_INITIAL_SECONDS,
        max_seconds: float = SYMBOL_BACKOFF_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._initial = initial_seconds
        self._max = max_seconds
        self._clock = clock
        self._until: dict[str, float] = {}
        self._delay: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        self._source: object | None = None

    def for_source(self, source: object) -> None:
        if self._source is not None and self._source is not source:
            self._until.clear()
            self._delay.clear()
            self._last_error.clear()
        self._source = source

    def remaining(self, symbol: str) -> float:
        return max(0.0, self._until.get(symbol, 0.0) - self._clock())

    def last_error(self, symbol: str) -> str:
        return self._last_error.get(symbol, "")

    def failed(self, symbol: str, exc: Exception) -> float:
        delay = min(self._delay.get(symbol, self._initial / 2) * 2, self._max)
        self._delay[symbol] = delay
        self._until[symbol] = self._clock() + delay
        self._last_error[symbol] = f"{type(exc).__name__}: {str(exc)[:_MAX_REASON_CHARS]}"
        return delay

    def succeeded(self, symbol: str) -> bool:
        """True if the symbol had been failing (so the caller can say it recovered)."""
        was_failing = symbol in self._delay
        self._until.pop(symbol, None)
        self._delay.pop(symbol, None)
        self._last_error.pop(symbol, None)
        return was_failing


class TickPublishError(Exception):
    """Some symbols' ticks could not be published this round; the rest were.
    `failures` maps each failed symbol to its exception, `published` holds the
    ticks that did go out. Raised only *after* every symbol has been tried, so
    the job that drives `publish_ticks_once` still fails honestly (its run
    history shows which symbols and why) without one bad symbol having starved
    the ones after it."""

    def __init__(self, failures: dict[str, Exception], published: list[Tick], total: int) -> None:
        self.failures = failures
        self.published = published
        shown = list(failures.items())[:_MAX_SYMBOLS_IN_ERROR]
        detail = "; ".join(
            f"{symbol} ({type(exc).__name__}: {str(exc)[:_MAX_REASON_CHARS]})"
            for symbol, exc in shown
        )
        more = len(failures) - len(shown)
        if more > 0:
            detail += f"; and {more} more"
        super().__init__(f"tick publish failed for {len(failures)} of {total} symbols: {detail}")


async def publish_ticks_once(
    redis: Redis,
    source: TickSource,
    symbols: list[str],
    *,
    backoff: SymbolBackoff | None = None,
) -> list[Tick]:
    """Publishes one tick per symbol -- the body of the APScheduler
    interval job driving whichever `TickSource` it was constructed with
    (real broker-quote polling or this module's mock fallback).

    Each symbol is attempted independently: a symbol the broker cannot quote
    must not stop every symbol after it from getting its tick (it used to --
    the first failure aborted the loop). If any failed, `TickPublishError` is
    raised once all have been tried. `TickSourceUnavailableError` is different:
    the whole source is down and says so, so it is re-raised at once, unwrapped.

    With a `backoff`, a symbol that failed is skipped (and reported, as a
    `SymbolBackedOffError`) until its back-off expires, instead of being asked
    about on every round.
    """
    if backoff is not None:
        backoff.for_source(source)
    ticks: list[Tick] = []
    failures: dict[str, Exception] = {}
    for symbol in symbols:
        if backoff is not None and (retry_in := backoff.remaining(symbol)) > 0:
            failures[symbol] = SymbolBackedOffError(symbol, retry_in, backoff.last_error(symbol))
            continue
        try:
            tick = await source.next_tick(symbol)
            await publish_tick(redis, tick)
        except TickSourceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolating one symbol's failure from the rest
            failures[symbol] = exc
            if backoff is not None:
                delay = backoff.failed(symbol, exc)
                logger.warning(
                    "tick_feed.symbol_failed",
                    symbol=symbol,
                    error=backoff.last_error(symbol),
                    next_attempt_in_seconds=delay,
                )
            continue
        if backoff is not None and backoff.succeeded(symbol):
            logger.info("tick_feed.symbol_recovered", symbol=symbol)
        ticks.append(tick)
    if failures:
        raise TickPublishError(failures, ticks, len(symbols)) from next(iter(failures.values()))
    return ticks
