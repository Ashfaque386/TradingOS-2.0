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
from dataclasses import dataclass, field
from typing import Protocol, cast

import numpy as np
from redis.asyncio import Redis


def tick_stream_key(symbol: str) -> str:
    return f"paper:ticks:{symbol}"


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    price: float
    timestamp_ms: int


async def publish_tick(redis: Redis, tick: Tick) -> str:
    # redis-py's own xadd() return type is untyped (Any) regardless of the
    # real ID string it returns.
    return cast(
        str,
        await redis.xadd(
            tick_stream_key(tick.symbol),
            {"price": repr(tick.price), "timestamp_ms": str(tick.timestamp_ms)},
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
        symbol=symbol, price=float(fields["price"]), timestamp_ms=int(fields["timestamp_ms"])
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
        return Tick(symbol=symbol, price=new_price, timestamp_ms=int(time.time() * 1000))


class TickSourceUnavailableError(Exception):
    """Raised by a `TickSource` that cannot produce a tick *and knows it will
    keep failing until something changes* (a broker that rejected the stored
    credentials). Distinct from an ordinary failure so a source can stop
    calling a service that has already said no, instead of retrying on every
    firing of the job that drives it."""


_MAX_SYMBOLS_IN_ERROR = 5
_MAX_REASON_CHARS = 120


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


async def publish_ticks_once(redis: Redis, source: TickSource, symbols: list[str]) -> list[Tick]:
    """Publishes one tick per symbol -- the body of the APScheduler
    interval job driving whichever `TickSource` it was constructed with
    (real broker-quote polling or this module's mock fallback).

    Each symbol is attempted independently: a symbol the broker cannot quote
    must not stop every symbol after it from getting its tick (it used to --
    the first failure aborted the loop). If any failed, `TickPublishError` is
    raised once all have been tried. `TickSourceUnavailableError` is different:
    the whole source is down and says so, so it is re-raised at once, unwrapped.
    """
    ticks: list[Tick] = []
    failures: dict[str, Exception] = {}
    for symbol in symbols:
        try:
            tick = await source.next_tick(symbol)
            await publish_tick(redis, tick)
        except TickSourceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolating one symbol's failure from the rest
            failures[symbol] = exc
            continue
        ticks.append(tick)
    if failures:
        raise TickPublishError(failures, ticks, len(symbols)) from next(iter(failures.values()))
    return ticks
