"""Wires Phase 6's `BrokerCircuitBreaker` (src.engine.risk.circuit_breaker)
to real broker adapters (Build Spec §13): `ResilientBrokerAdapter` wraps
any `BrokerAdapter` and routes every network-calling method through one
breaker instance, so 3 consecutive `BrokerServerError`s (a 5xx) open the
circuit and further calls are refused for a 1-minute cooldown rather than
hammering a broker that's already down. `build_order_payload` is a pure,
local, no-network method and deliberately bypasses the breaker entirely
-- it can't fail against the broker at all, so counting it would be
meaningless.

This is the same `BrokerCircuitBreaker` class Phase 6 built and tested
against its own `FakeBrokerAdapter` -- nothing here reimplements the
open/cooldown/half-open state machine, only adapts the richer Phase 8
`BrokerAdapter` surface (nine network-calling methods, not just
`send_order`) onto that one breaker's generic `call(fn)`.
"""

import time

from src.brokers.base import (
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderResult,
    BrokerPosition,
    BrokerQuote,
    MarginInfo,
    OptionChainEntry,
    OrderRequest,
)
from src.core.config import get_settings
from src.engine.risk.circuit_breaker import BrokerCircuitBreaker
from src.observability.metrics import (
    order_dispatch_budget_breached_total,
    order_dispatch_latency_seconds,
)


class ResilientBrokerAdapter:
    def __init__(self, adapter: BrokerAdapter, breaker: BrokerCircuitBreaker | None = None):
        self._adapter = adapter
        self._breaker = breaker if breaker is not None else BrokerCircuitBreaker()

    @property
    def broker_name(self) -> str:
        return self._adapter.broker_name

    @property
    def has_sandbox(self) -> bool:
        return self._adapter.has_sandbox

    @property
    def breaker(self) -> BrokerCircuitBreaker:
        return self._breaker

    def build_order_payload(self, order: OrderRequest) -> dict:
        return self._adapter.build_order_payload(order)

    async def place_order(self, order: OrderRequest) -> BrokerOrderResult:
        started = time.perf_counter()
        try:
            return await self._breaker.call(lambda: self._adapter.place_order(order))
        finally:
            elapsed_seconds = time.perf_counter() - started
            order_dispatch_latency_seconds.labels(broker=self._adapter.broker_name).observe(
                elapsed_seconds
            )
            budget_ms = get_settings().order_dispatch_latency_budget_ms
            if elapsed_seconds * 1000 > budget_ms:
                order_dispatch_budget_breached_total.labels(broker=self._adapter.broker_name).inc()

    async def modify_order(
        self, broker_order_id: str, *, price: float | None = None, quantity: int | None = None
    ) -> BrokerOrderResult:
        return await self._breaker.call(
            lambda: self._adapter.modify_order(broker_order_id, price=price, quantity=quantity)
        )

    async def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        return await self._breaker.call(lambda: self._adapter.cancel_order(broker_order_id))

    async def get_order_book(self) -> list[BrokerOrder]:
        return await self._breaker.call(self._adapter.get_order_book)

    async def get_margin(self) -> MarginInfo:
        return await self._breaker.call(self._adapter.get_margin)

    async def get_positions(self) -> list[BrokerPosition]:
        return await self._breaker.call(self._adapter.get_positions)

    async def get_quote(self, symbol: str) -> BrokerQuote:
        return await self._breaker.call(lambda: self._adapter.get_quote(symbol))

    async def get_option_chain(self, underlying: str, expiry: str) -> list[OptionChainEntry]:
        return await self._breaker.call(lambda: self._adapter.get_option_chain(underlying, expiry))

    async def get_expiries(self, underlying: str) -> list[str]:
        return await self._breaker.call(lambda: self._adapter.get_expiries(underlying))
