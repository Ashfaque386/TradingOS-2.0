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

from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.engine.paper_trading.tick_feed import MockTickSource, Tick, TickSource


@dataclass
class BrokerQuoteTickSource:
    adapter: BrokerAdapter

    async def next_tick(self, symbol: str) -> Tick:
        quote = await self.adapter.get_quote(symbol)
        return Tick(symbol=symbol, price=quote.last_price, timestamp_ms=int(time.time() * 1000))


def build_tick_source() -> TickSource:
    adapter = build_configured_adapter(sandbox=False)
    if adapter is None:
        return MockTickSource()
    return BrokerQuoteTickSource(adapter)
