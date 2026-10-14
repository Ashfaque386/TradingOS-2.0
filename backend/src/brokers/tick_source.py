"""Real broker-quote polling (Build Spec §13), replacing Phase 7's
`MockTickSource` as the `TickSource` the paper-trading tick-publish job
drives: `BrokerQuoteTickSource.next_tick` fetches one live quote per call
via `BrokerAdapter.get_quote` and turns it into a `Tick`, instead of
advancing a synthetic random walk.

`build_tick_source()` is the one place that decides which `TickSource`
implementation actually runs: it delegates credential lookup and adapter
construction to `src.brokers.factory.build_configured_adapter` (shared
with Phase 9's live-order submission path) and, if a broker is
configured, wraps it in `BrokerQuoteTickSource`; otherwise it falls back
to `MockTickSource`. Swapping in real credentials later (via the
broker-credentials API) changes this function's output with no change at
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
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from src.brokers import feed_health
from src.brokers.base import BrokerAdapter, BrokerAuthError, BrokerCredentials
from src.brokers.factory import KNOWN_BROKERS, build_configured_adapter
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    Tick,
    TickSource,
    TickSourceUnavailableError,
)
from src.models.data_source import DataSource
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)


# How long a source stays quiet after its broker rejected the credentials
# before it risks one probe call. A reconnect in Settings is picked up at once
# (`get_tick_source` builds a fresh source when the credentials change); this
# only covers a fix made on the broker's side with the stored credentials
# unchanged, at the price of a handful of calls an hour instead of one every
# 3 seconds.
AUTH_REJECTED_RETRY_SECONDS = 600.0


@dataclass
class BrokerQuoteTickSource:
    adapter: BrokerAdapter
    retry_after_seconds: float = AUTH_REJECTED_RETRY_SECONDS
    clock: Callable[[], float] = time.monotonic
    _suspended_until: float | None = field(default=None, init=False, repr=False)
    _rejection: str = field(default="", init=False, repr=False)

    async def next_tick(self, symbol: str) -> Tick:
        now = self.clock()
        if self._suspended_until is not None and now < self._suspended_until:
            raise TickSourceUnavailableError(
                f"{self._rejection}; not polling quotes until the credentials change "
                f"(next probe in {self._suspended_until - now:.0f}s)"
            )
        try:
            quote = await self.adapter.get_quote(symbol)
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
        return Tick(
            symbol=symbol,
            price=quote.last_price,
            timestamp_ms=int(time.time() * 1000),
            source=DataSource.REAL,  # a live quote from a real broker
        )


def build_tick_source() -> TickSource:
    adapter = build_configured_adapter(sandbox=False)
    if adapter is None:
        logger.info("tick_source.selected", source="mock", reason="no broker configured")
        return MockTickSource()
    logger.info("tick_source.selected", source="broker_quotes", adapter=type(adapter).__name__)
    return BrokerQuoteTickSource(adapter)


def _configured_broker_fingerprint() -> tuple[str, BrokerCredentials] | None:
    """The same "which broker is configured" lookup `build_configured_adapter`
    does, plus the credentials themselves (a frozen, structurally-`==`-able
    dataclass) so re-saving the *same* broker's key/token is also detected
    as a real change, not just switching between brokers."""
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return None
    for broker in KNOWN_BROKERS:
        credentials = store.get_credentials(broker)
        if credentials is not None:
            return (broker, credentials)
    return None


class _RefreshingTickSource:
    def __init__(self) -> None:
        self._fingerprint: tuple[str, BrokerCredentials] | None = None
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
