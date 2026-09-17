"""Circuit breaker wiring tests (Build Spec §13): Phase 6's
BrokerCircuitBreaker, wrapped around a real adapter (not a fake), opens
after 3 consecutive 5xx responses and respects the cooldown -- proving
`ResilientBrokerAdapter` actually connects the two, not just that the
breaker works in isolation (Phase 6 already tested that against its own
FakeBrokerAdapter).
"""

import httpx
import pytest

from src.brokers.base import BrokerCredentials, OrderRequest, OrderSide
from src.brokers.resilient import ResilientBrokerAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.risk.circuit_breaker import (
    BrokerCircuitBreaker,
    CircuitOpenError,
    CircuitState,
)

_CREDENTIALS = BrokerCredentials(api_key="test-key", access_token="test-token")
_ORDER = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=10)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _make_resilient_adapter(status_codes: list[int], clock) -> ResilientBrokerAdapter:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        code = status_codes[min(calls["n"], len(status_codes) - 1)]
        calls["n"] += 1
        if code == 200:
            return httpx.Response(200, json={"data": {"order_id": "OID1"}})
        return httpx.Response(code, text="simulated broker failure")

    inner = ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(handler))
    breaker = BrokerCircuitBreaker(cooldown_seconds=60.0, clock=clock)
    return ResilientBrokerAdapter(inner, breaker)


async def test_circuit_opens_after_three_consecutive_5xx():
    clock = _FakeClock()
    adapter = _make_resilient_adapter([503, 503, 503], clock)

    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017, PT011 - BrokerServerError, re-raised as-is
            await adapter.place_order(_ORDER)

    assert adapter.breaker.state == CircuitState.OPEN
    assert adapter.breaker.consecutive_failures == 3


async def test_open_circuit_refuses_calls_until_cooldown_elapses():
    clock = _FakeClock()
    adapter = _make_resilient_adapter([503, 503, 503, 200], clock)

    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017, PT011
            await adapter.place_order(_ORDER)
    assert adapter.breaker.state == CircuitState.OPEN

    # Still inside the cooldown -- refused without even reaching the
    # broker (the 4th, would-succeed response is never consumed).
    clock.now = 30.0
    with pytest.raises(CircuitOpenError):
        await adapter.place_order(_ORDER)

    # Cooldown elapsed -- one trial call is let through and succeeds.
    clock.now = 61.0
    result = await adapter.place_order(_ORDER)
    assert result.broker_order_id == "OID1"
    assert adapter.breaker.state == CircuitState.CLOSED


async def test_a_4xx_never_trips_the_breaker():
    """The circuit breaker only reacts to BrokerServerError (5xx) -- a
    string of 4xx client errors (bad symbol, expired token) must never
    open the circuit, since that would be the caller's own mistake, not a
    broker outage."""
    clock = _FakeClock()
    adapter = _make_resilient_adapter([400, 400, 400, 400], clock)

    for _ in range(4):
        with pytest.raises(Exception):  # noqa: B017, PT011 - BrokerRequestError
            await adapter.place_order(_ORDER)

    assert adapter.breaker.state == CircuitState.CLOSED
    assert adapter.breaker.consecutive_failures == 0


async def test_build_order_payload_bypasses_the_breaker_entirely():
    """A pure, local, no-network method -- it can never fail against the
    broker, so it must never touch the breaker's failure counter."""
    clock = _FakeClock()
    adapter = _make_resilient_adapter([503, 503, 503], clock)

    for _ in range(10):
        adapter.build_order_payload(_ORDER)

    assert adapter.breaker.state == CircuitState.CLOSED
    assert adapter.breaker.consecutive_failures == 0


def _histogram_sample_count(histogram, **labels) -> float:
    """These metrics are process-global singletons (src/observability/
    metrics.py's module-level objects), shared across the whole test
    session -- `collect()` returns samples for every label combination any
    test has ever recorded, so a before/after comparison must match on the
    exact label set, not just take the first `_count` sample it finds.
    """
    for sample in histogram.collect()[0].samples:
        if sample.name.endswith("_count") and sample.labels == labels:
            return sample.value
    return 0.0


async def test_place_order_records_the_order_dispatch_latency_metric():
    """Phase 11 (Build Spec §19): every order dispatch is observed against
    the tradingos_order_dispatch_latency_seconds histogram, labeled by
    broker -- the metric fires even on a successful call, not just a
    failure."""
    from src.observability.metrics import order_dispatch_latency_seconds

    clock = _FakeClock()
    adapter = _make_resilient_adapter([200], clock)

    before = _histogram_sample_count(order_dispatch_latency_seconds, broker="zerodha")
    await adapter.place_order(_ORDER)
    after = _histogram_sample_count(order_dispatch_latency_seconds, broker="zerodha")

    assert after == before + 1


async def test_place_order_increments_budget_breach_counter_when_over_budget(monkeypatch):
    """A dispatch that exceeds settings.order_dispatch_latency_budget_ms
    increments the breach counter; a real, non-fabricated comparison
    against the documented budget, not just a latency observation."""
    from src.brokers import resilient as resilient_module
    from src.core.config import get_settings
    from src.observability import metrics as metrics_module

    class _TinyBudgetSettings:
        order_dispatch_latency_budget_ms = -1.0  # guaranteed to be exceeded by any real call

    monkeypatch.setattr(resilient_module, "get_settings", lambda: _TinyBudgetSettings())

    clock = _FakeClock()
    adapter = _make_resilient_adapter([200], clock)
    counter_child = metrics_module.order_dispatch_budget_breached_total.labels(broker="zerodha")

    before = counter_child._value.get()
    await adapter.place_order(_ORDER)
    after = counter_child._value.get()

    assert after == before + 1
    # get_settings itself is untouched -- only this module's imported
    # reference was monkeypatched.
    assert get_settings().order_dispatch_latency_budget_ms > 0
