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
to `MockTickSource`. This sandbox has no live broker credentials
configured, so `build_tick_source()` always resolves to the mock path
here and in CI -- same honest-stub posture as every other
not-yet-credentialed external integration in this codebase. Swapping in
real credentials later (via the broker-credentials API) changes this
function's output with no change at any call site, same Protocol-swap
posture as every other provider in `src.engine.paper_trading`.
"""

import time
from dataclasses import dataclass

import structlog

from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.engine.paper_trading.tick_feed import MockTickSource, Tick, TickSource
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)


@dataclass
class BrokerQuoteTickSource:
    adapter: BrokerAdapter

    async def next_tick(self, symbol: str) -> Tick:
        quote = await self.adapter.get_quote(symbol)
        return Tick(symbol=symbol, price=quote.last_price, timestamp_ms=int(time.time() * 1000))


def build_tick_source() -> TickSource:
    # Resolved once, at startup (src.main's lifespan) -- a broker connected
    # later via Settings only takes effect on the next backend restart, so
    # this line is how an operator confirms which feed is actually live.
    adapter = build_configured_adapter(sandbox=False)
    if adapter is None:
        logger.info("tick_source.selected", source="mock", reason="no broker configured")
        return MockTickSource()
    logger.info("tick_source.selected", source="broker_quotes", adapter=type(adapter).__name__)
    return BrokerQuoteTickSource(adapter)


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
