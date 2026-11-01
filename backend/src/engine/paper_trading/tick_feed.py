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

import json
import time
import zlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import numpy as np
import structlog
from redis.asyncio import Redis

from src.engine.paper_trading.depth import DepthSnapshot, publish_depth
from src.engine.risk.compliance import ExchangeCircuitLimits
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
    # The broker's order book at the moment of this tick, when the source had one. It is not part
    # of the stream entry: `publish_tick` stores it as the symbol's latest depth snapshot.
    depth: DepthSnapshot | None = field(default=None, compare=False)
    # When the exchange stamped the price (epoch milliseconds), and the exchange's circuit limits
    # for the day, where the broker sent them. `None` is "not known", never a guess.
    exchange_time_ms: int | None = None
    lower_circuit: float | None = None
    upper_circuit: float | None = None


def tick_circuit_limits(tick: Tick) -> ExchangeCircuitLimits | None:
    """The exchange's circuit limits carried on this tick, or `None` if it carried none."""
    if tick.lower_circuit is None or tick.upper_circuit is None:
        return None
    return ExchangeCircuitLimits(lower=tick.lower_circuit, upper=tick.upper_circuit)


def _stream_fields(tick: Tick) -> dict[Any, Any]:
    entry = {
        "price": repr(tick.price),
        "timestamp_ms": str(tick.timestamp_ms),
        "source": tick.source,
    }
    if tick.exchange_time_ms is not None:
        entry["exchange_ms"] = str(tick.exchange_time_ms)
    if tick.lower_circuit is not None and tick.upper_circuit is not None:
        entry["lower_circuit"] = repr(tick.lower_circuit)
        entry["upper_circuit"] = repr(tick.upper_circuit)
    return entry


def _tick_from_fields(symbol: str, fields: Mapping[str, str]) -> Tick:
    # Entries written before a field existed carry none of it.
    return Tick(
        symbol=symbol,
        price=float(fields["price"]),
        timestamp_ms=int(fields["timestamp_ms"]),
        source=fields.get("source", "unknown"),
        exchange_time_ms=int(fields["exchange_ms"]) if "exchange_ms" in fields else None,
        lower_circuit=float(fields["lower_circuit"]) if "lower_circuit" in fields else None,
        upper_circuit=float(fields["upper_circuit"]) if "upper_circuit" in fields else None,
    )


async def publish_tick(
    redis: Redis,
    tick: Tick,
    *,
    maxlen: int | None = None,
    depth_ttl_seconds: float = 60.0,
) -> str:
    """Appends the tick to its symbol's stream, trimmed to about `maxlen` entries (oldest go
    first; `None` leaves the stream untrimmed), and stores its depth, if it has any, as the
    symbol's latest book."""
    # redis-py's own xadd() return type is untyped (Any) regardless of the
    # real ID string it returns.
    entry_id = cast(
        str,
        await redis.xadd(
            tick_stream_key(tick.symbol),
            _stream_fields(tick),
            maxlen=maxlen,
            approximate=True,
        ),
    )
    if tick.depth is not None and not tick.depth.is_empty():
        await publish_depth(redis, tick.symbol, tick.depth, ttl_seconds=depth_ttl_seconds)
    return entry_id


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
        ticks.append(_tick_from_fields(symbol, fields))
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
    return _tick_from_fields(symbol, fields)


async def get_latest_tick_id(redis: Redis, symbol: str) -> str | None:
    """The stream id of the newest tick for `symbol`, or `None` for an empty stream. A
    subscription that is switched back on starts its cursor here, so it does not drain (and act
    on) every tick published while it was off."""
    entries = await redis.xrevrange(tick_stream_key(symbol), count=1)
    if not entries:
        return None
    entry_id, _fields = entries[0]
    return str(entry_id)


class TickSource(Protocol):
    async def next_tick(self, symbol: str) -> Tick: ...


# What a batching source reports for one symbol: its tick, or why it has none.
TickOutcome = Tick | Exception


class BatchTickSource(TickSource, Protocol):
    """A source that can quote many symbols in one round trip. `publish_ticks_once` prefers
    `next_ticks` when a source has it: one broker call per cycle instead of one per symbol."""

    async def next_ticks(self, symbols: Sequence[str]) -> dict[str, TickOutcome]: ...


class NoQuoteError(Exception):
    """The source answered but had no tick for this symbol."""


class QuoteRefusedError(Exception):
    """The broker answered, and what it answered is not a price to act on (its exchange stamp is
    from a previous day, or older than the limit). Distinct from a failure: nothing is wrong with
    the broker or the symbol, so it is not backed off (the next cycle asks again, and the day the
    exchange trades the quote is accepted at once) and it does not fall through to another broker,
    whose answer would be no fresher."""

    def __init__(
        self, *, symbol: str, broker: str, reason: str, exchange_time: datetime | None = None
    ) -> None:
        self.symbol = symbol
        self.broker = broker
        self.reason = reason
        self.exchange_time = exchange_time
        super().__init__(f"{broker} quote for {symbol} refused: {reason}")


# How long a refusal is remembered for the desk to show. Refused again every cycle while it lasts,
# so it disappears half a minute after the quotes become acceptable.
QUOTE_REFUSAL_TTL_SECONDS = 30


def quote_refusal_key(symbol: str) -> str:
    return f"paper:quote_refusal:{symbol}"


@dataclass(frozen=True, slots=True)
class QuoteRefusal:
    broker: str
    reason: str
    exchange_time: datetime | None
    at: datetime


async def note_quote_refusal(redis: Redis, error: QuoteRefusedError) -> None:
    """Remembers, for `QUOTE_REFUSAL_TTL_SECONDS`, that this symbol's quote was refused and why."""
    payload = {
        "broker": error.broker,
        "reason": error.reason,
        "exchange_time": error.exchange_time.isoformat() if error.exchange_time else None,
        "at": datetime.now(UTC).isoformat(),
    }
    await redis.set(
        quote_refusal_key(error.symbol), json.dumps(payload), ex=QUOTE_REFUSAL_TTL_SECONDS
    )


async def get_quote_refusal(redis: Redis, symbol: str) -> QuoteRefusal | None:
    """Why this symbol's newest quote was refused, or `None` if it was not (recently)."""
    raw = await redis.get(quote_refusal_key(symbol))
    if not raw:
        return None
    data: dict[str, Any] = json.loads(raw)
    stamp = data.get("exchange_time")
    return QuoteRefusal(
        broker=data["broker"],
        reason=data["reason"],
        exchange_time=datetime.fromisoformat(stamp) if stamp else None,
        at=datetime.fromisoformat(data["at"]),
    )


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


async def _outcomes(
    source: TickSource, symbols: list[str]
) -> AsyncIterator[tuple[str, TickOutcome]]:
    """Asks `source` about `symbols` and yields each one's outcome: all at once after one batched
    call if the source can, else one call per symbol as the caller consumes them (so what was
    published before a source went down stays published). A failure of one symbol is that
    symbol's outcome, not the others'. A source that is down for good
    (`TickSourceUnavailableError`) raises."""
    batch = getattr(source, "next_ticks", None)
    if batch is not None:
        try:
            got = cast(dict[str, TickOutcome], await batch(symbols))
        except TickSourceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - the call failed for every symbol in it
            for symbol in symbols:
                yield symbol, exc
            return
        for symbol in symbols:
            yield symbol, got.get(symbol, NoQuoteError(f"no quote for {symbol}"))
        return
    for symbol in symbols:
        try:
            outcome: TickOutcome = await source.next_tick(symbol)
        except TickSourceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolating one symbol's failure from the rest
            outcome = exc
        yield symbol, outcome


async def publish_ticks_once(
    redis: Redis,
    source: TickSource,
    symbols: list[str],
    *,
    backoff: SymbolBackoff | None = None,
    maxlen: int | None = None,
    depth_ttl_seconds: float = 60.0,
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
    asking: list[str] = []
    for symbol in symbols:
        if backoff is not None and (retry_in := backoff.remaining(symbol)) > 0:
            failures[symbol] = SymbolBackedOffError(symbol, retry_in, backoff.last_error(symbol))
            continue
        asking.append(symbol)
    async for symbol, outcome in _outcomes(source, asking):
        try:
            if isinstance(outcome, Exception):
                raise outcome
            await publish_tick(redis, outcome, maxlen=maxlen, depth_ttl_seconds=depth_ttl_seconds)
        except QuoteRefusedError as refused:
            # Not a failure to back off from: ask again next cycle (see the error's docstring).
            failures[symbol] = refused
            await note_quote_refusal(redis, refused)
            continue
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
        ticks.append(outcome)
    if failures:
        raise TickPublishError(failures, ticks, len(symbols)) from next(iter(failures.values()))
    return ticks
