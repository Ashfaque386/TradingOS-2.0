"""Real broker-quote polling (Build Spec §13), replacing Phase 7's
`MockTickSource` as the `TickSource` the paper-trading tick-publish job
drives: `BrokerQuoteTickSource.next_tick` fetches one live quote per call
via `BrokerAdapter.get_quote` and turns it into a `Tick`, instead of
advancing a synthetic random walk.

`build_tick_source()` is the one place that decides which `TickSource`
implementation actually runs: it delegates credential lookup and adapter
construction to `src.brokers.factory.build_configured_adapter` (shared
with Phase 9's live-order submission path) and, if a broker is
configured, wraps it in `BrokerQuoteTickSource`; otherwise it returns an
`UnavailableTickSource` that says so on every call -- it no longer falls back to
`MockTickSource`, which would have paper-traded real symbols on a random walk. The mock
is only used when `PAPER_TRADING_SYNTHETIC` is set (demo/tests). Swapping in real credentials later
(via the broker-credentials API) changes this function's output with no change at
any call site, same Protocol-swap posture as every other provider in
`src.engine.paper_trading`.

`get_tick_source()` (not `build_tick_source()` directly) is the real
production entry point (`src.main`'s `tick_source_factory=get_tick_source`):
it caches the last-built `TickSource` and only calls `build_tick_source()`
again when the configured broker -- or that broker's own credentials --
has actually changed since the previous call (Phase 17 Part 4 real-world
testing fix). Two things would break if this rebuilt on every tick-publish
firing instead: a `BrokerQuoteTickSource`'s wrapped adapter would keep
re-resolving the same shared `BrokerCircuitBreaker` from
`breaker_registry` (harmless, since that registry is itself the real
state), but a `MockTickSource`'s own per-symbol random walk
(`_rng_by_symbol`/`_last_price_by_symbol`) is instance state with no
external backing store -- rebuilding it every 3 seconds would silently
reset every mock price back to `default_base_price` forever, never
actually walking. Caching by a fingerprint of "which broker, with which
credentials" avoids that while still closing the original bug: a broker
connected or disconnected later via Settings now takes effect on this
job's very next firing, not only after a backend restart.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from src.brokers import feed_health
from src.brokers.base import BrokerAdapter, BrokerAuthError, BrokerCredentials, BrokerQuote
from src.brokers.factory import KNOWN_BROKERS
from src.core.config import get_settings
from src.data.market_data_factory import connected_adapters
from src.engine.paper_trading.depth import DepthSnapshot
from src.engine.paper_trading.order_book import OrderBookLevel
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    NoQuoteError,
    Tick,
    TickOutcome,
    TickSource,
    TickSourceUnavailableError,
    UnavailableTickSource,
)
from src.models.data_source import DataSource
from src.observability.metrics import (
    paper_quote_exchange_lag_seconds,
    paper_quote_exchange_lag_unusable_total,
    paper_quote_fetch_seconds,
)
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)


# How long a source stays quiet after its broker rejected the credentials
# before it risks one probe call. A reconnect in Settings is picked up at once
# (`get_tick_source` builds a fresh source when the credentials change); this
# only covers a fix made on the broker's side with the stored credentials
# unchanged, at the price of a handful of calls an hour instead of one every
# 3 seconds.
AUTH_REJECTED_RETRY_SECONDS = 600.0

# A quote stamped slightly ahead of this machine's clock is clock skew, not a future quote.
EXCHANGE_CLOCK_SKEW_SECONDS = 2.0


@dataclass
class BrokerQuoteTickSource:
    adapter: BrokerAdapter
    retry_after_seconds: float = AUTH_REJECTED_RETRY_SECONDS
    clock: Callable[[], float] = time.monotonic
    _suspended_until: float | None = field(default=None, init=False, repr=False)
    _rejection: str = field(default="", init=False, repr=False)

    async def next_tick(self, symbol: str) -> Tick:
        quote = await self._guarded(lambda: self.adapter.get_quote(symbol))
        self._record_exchange_lag(quote)
        return _tick_from_quote(quote)

    async def next_ticks(self, symbols: Sequence[str]) -> dict[str, Tick]:
        """One batched quote call for all `symbols`. A symbol the broker has no price for is
        simply absent from the result."""
        quotes = await self._guarded(lambda: self.adapter.get_quotes(symbols))
        for quote in quotes.values():
            self._record_exchange_lag(quote)
        return {symbol: _tick_from_quote(quote) for symbol, quote in quotes.items()}

    def _record_exchange_lag(self, quote: BrokerQuote) -> None:
        """How old the exchange's own stamp on this quote was when it reached us. Recorded only
        where the broker sends one we can read; a missing, unreadable or future stamp is counted,
        not turned into a number."""
        broker = self.adapter.broker_name
        if quote.exchange_time is None:
            paper_quote_exchange_lag_unusable_total.labels(broker=broker, reason="absent").inc()
            return
        lag = (datetime.now(UTC) - quote.exchange_time).total_seconds()
        if lag < -EXCHANGE_CLOCK_SKEW_SECONDS:
            paper_quote_exchange_lag_unusable_total.labels(broker=broker, reason="future").inc()
            return
        paper_quote_exchange_lag_seconds.labels(broker=broker).observe(max(lag, 0.0))

    async def _timed[T](self, call: Callable[[], Awaitable[T]]) -> T:
        started = time.perf_counter()
        outcome = "error"
        try:
            result = await call()
            outcome = "ok"
            return result
        finally:
            paper_quote_fetch_seconds.labels(
                broker=self.adapter.broker_name, outcome=outcome
            ).observe(time.perf_counter() - started)

    async def _guarded[T](self, call: Callable[[], Awaitable[T]]) -> T:
        now = self.clock()
        if self._suspended_until is not None and now < self._suspended_until:
            raise TickSourceUnavailableError(
                f"{self._rejection}; not polling quotes until the credentials change "
                f"(next probe in {self._suspended_until - now:.0f}s)"
            )
        try:
            result = await self._timed(call)
        except BrokerAuthError as exc:
            # Said once, here, at the moment it happens -- not on every firing.
            self._rejection = (
                f"{self.adapter.broker_name} rejected the stored credentials "
                f"(HTTP {exc.status_code})"
            )
            self._suspended_until = now + self.retry_after_seconds
            feed_health.record_rejection(
                self.adapter.broker_name, status_code=exc.status_code, detail=str(exc)
            )
            logger.warning(
                "tick_source.broker_credentials_rejected",
                broker=self.adapter.broker_name,
                status=exc.status_code,
                action="reconnect the broker in Settings > Broker",
                retry_after_seconds=self.retry_after_seconds,
                detail=str(exc),
            )
            raise TickSourceUnavailableError(self._rejection) from exc
        if self._suspended_until is not None:
            self._suspended_until = None
            feed_health.clear(self.adapter.broker_name)
            logger.info(
                "tick_source.broker_credentials_accepted_again", broker=self.adapter.broker_name
            )
        return result


def _tick_from_quote(quote: BrokerQuote) -> Tick:
    now_ms = int(time.time() * 1000)
    depth = None
    if quote.depth_bids or quote.depth_asks:
        depth = DepthSnapshot(
            bids=tuple(OrderBookLevel(level.price, level.quantity) for level in quote.depth_bids),
            asks=tuple(OrderBookLevel(level.price, level.quantity) for level in quote.depth_asks),
            timestamp_ms=now_ms,
            open_interest=quote.open_interest,
        )
    return Tick(
        symbol=quote.symbol,
        price=quote.last_price,
        timestamp_ms=now_ms,
        source=DataSource.REAL,  # a live quote from a real broker
        depth=depth,
    )


class FallbackTickSource:
    """Live quotes from the first connected broker, with the next one as the fallback.

    Each cycle asks the preferred broker for every symbol in one batched call. Whatever it does
    not answer -- symbols it has no price for, or all of them if it is down, refused the
    credentials or its circuit is open -- is asked of the next broker, and so on. A symbol no
    broker prices is reported as failed (the caller backs it off); if every broker is
    unavailable the whole source is, and says why. The data is real whichever broker served it.
    """

    def __init__(self, sources: Sequence[BrokerQuoteTickSource]) -> None:
        self.sources = list(sources)
        self._served_by: str | None = None

    async def next_tick(self, symbol: str) -> Tick:
        outcome = (await self.next_ticks([symbol]))[symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def next_ticks(self, symbols: Sequence[str]) -> dict[str, TickOutcome]:
        outcomes: dict[str, TickOutcome] = {}
        remaining = list(dict.fromkeys(symbols))
        unavailable: list[str] = []
        last_error: Exception | None = None
        served_by: str | None = None
        for source in self.sources:
            if not remaining:
                break
            try:
                ticks = await source.next_ticks(remaining)
            except TickSourceUnavailableError as exc:
                unavailable.append(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - a broker outage: try the next broker
                last_error = exc
                logger.warning(
                    "tick_source.broker_failed",
                    broker=source.adapter.broker_name,
                    error=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
                continue
            if ticks:
                served_by = served_by or source.adapter.broker_name
            outcomes.update(ticks)
            remaining = [symbol for symbol in remaining if symbol not in ticks]
        if len(unavailable) == len(self.sources) and not outcomes:
            raise TickSourceUnavailableError("; ".join(unavailable))
        for symbol in remaining:
            outcomes[symbol] = last_error or NoQuoteError(f"no connected broker quotes {symbol}")
        if served_by is not None and served_by != self._served_by:
            logger.info("tick_source.serving_quotes", broker=served_by, previous=self._served_by)
            self._served_by = served_by
        return outcomes


def build_tick_source() -> TickSource:
    adapters = connected_adapters()
    if not adapters:
        if get_settings().paper_trading_synthetic:
            logger.info(
                "tick_source.selected", source="mock", reason="no broker; synthetic mode is on"
            )
            return MockTickSource()
        logger.info("tick_source.selected", source="unavailable", reason="no broker connected")
        return UnavailableTickSource()
    logger.info(
        "tick_source.selected",
        source="broker_quotes",
        brokers=[adapter.broker_name for adapter in adapters],
    )
    return FallbackTickSource([BrokerQuoteTickSource(adapter) for adapter in adapters])


def _configured_broker_fingerprint() -> tuple[tuple[str, BrokerCredentials], ...] | None:
    """Every broker with stored credentials, in preference order, with the credentials
    themselves (frozen, structurally-`==`-able dataclasses) so re-saving the *same* broker's
    key/token, or connecting a second broker, is detected as a real change."""
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return None
    configured = tuple(
        (broker, credentials)
        for broker in KNOWN_BROKERS
        if (credentials := store.get_credentials(broker)) is not None
    )
    return configured or None


class _RefreshingTickSource:
    def __init__(self) -> None:
        self._fingerprint: tuple[tuple[str, BrokerCredentials], ...] | None = None
        self._tick_source: TickSource | None = None

    def __call__(self) -> TickSource:
        fingerprint = _configured_broker_fingerprint()
        if self._tick_source is None or fingerprint != self._fingerprint:
            # New credentials (or none): whatever the broker said about the old ones
            # no longer applies.
            feed_health.clear()
            self._fingerprint = fingerprint
            self._tick_source = build_tick_source()
        return self._tick_source


_refreshing_tick_source = _RefreshingTickSource()


def get_tick_source() -> TickSource:
    return _refreshing_tick_source()


def feed_unavailable_reason() -> str | None:
    """Why paper trading has no real price feed right now, or `None` if it has one. Cheap and
    network-free (secrets store + the in-process rejection registry), so list endpoints can call
    it per request. A feed in synthetic demo mode is "available"."""
    fingerprint = _configured_broker_fingerprint()
    if fingerprint is None:
        return None if get_settings().paper_trading_synthetic else "no broker is connected"
    reasons: list[str] = []
    for broker, credentials in fingerprint:
        if not credentials.access_token:
            reasons.append(f"{broker} has keys saved but is not connected")
        elif credentials.token_status == "expired":  # nosec B105: a status label, not a secret
            reasons.append(f"the {broker} token has expired")
        elif (rejection := feed_health.get_rejection(broker)) is not None:
            reasons.append(
                f"{broker} rejected the stored credentials (HTTP {rejection.status_code})"
            )
        else:
            return None  # one usable broker is a feed
    return "; ".join(reasons)


def is_broker_configured() -> bool:
    """Cheap, no-network check for read paths (e.g. GET
    /api/v1/paper-trading/pnl/unrealized) that need to honestly label a
    figure as "real" vs "synthetic" without building a full adapter or
    touching the circuit-breaker registry the way `build_tick_source()`
    does -- same credential lookup `build_configured_adapter` uses
    (`SecretsStore.list_configured_brokers`), just without constructing
    anything. `SECRETS_ENCRYPTION_KEY` unset (the store itself unusable)
    is honestly "no broker configured", matching
    `build_configured_adapter`'s own `SecretsStoreError` handling."""
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return False
    return bool(store.list_configured_brokers())
