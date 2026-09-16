"""Broker Circuit Breaker tests (Build Spec §8): opens after 3 consecutive
5xx responses, refuses calls during a 1-minute cooldown, and lets a trial
call through once the cooldown elapses -- exercised against a fake broker
adapter and an injectable clock (no real sleeping).
"""

import pytest

from src.engine.risk.circuit_breaker import (
    BrokerCircuitBreaker,
    BrokerServerError,
    CircuitOpenError,
    CircuitState,
    FakeBrokerAdapter,
)


def _fake_clock():
    time_box = {"t": 0.0}

    def clock() -> float:
        return time_box["t"]

    def advance(seconds: float) -> None:
        time_box["t"] += seconds

    return clock, advance


async def test_stays_closed_below_the_failure_threshold():
    clock, _advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=2)

    for _ in range(2):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 2


async def test_opens_after_three_consecutive_failures_and_fires_the_alert_hook():
    clock, _advance = _fake_clock()
    events = []
    breaker = BrokerCircuitBreaker(
        failure_threshold=3, cooldown_seconds=60.0, on_open=events.append, clock=clock
    )
    adapter = FakeBrokerAdapter(remaining_failures=3)

    for _ in range(3):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    assert breaker.state == CircuitState.OPEN
    assert len(events) == 1
    assert events[0].consecutive_failures == 3


async def test_open_circuit_refuses_calls_without_invoking_the_adapter():
    clock, _advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=3)

    for _ in range(3):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    calls_before = adapter.calls
    with pytest.raises(CircuitOpenError):
        await breaker.call(adapter.send_order)
    assert adapter.calls == calls_before


async def test_cooldown_allows_a_trial_call_through_and_recovers_on_success():
    clock, advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=3)

    for _ in range(3):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)
    assert breaker.state == CircuitState.OPEN

    advance(61.0)
    adapter.remaining_failures = 0
    result = await breaker.call(adapter.send_order)

    assert result == {"status": "ok"}
    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


async def test_a_failed_trial_call_reopens_the_circuit():
    clock, advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=4)

    for _ in range(3):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    advance(61.0)
    with pytest.raises(BrokerServerError):
        await breaker.call(adapter.send_order)

    assert breaker.state == CircuitState.OPEN


async def test_non_server_errors_do_not_affect_the_breaker():
    clock, _advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)

    async def raises_value_error():
        raise ValueError("not a broker 5xx")

    for _ in range(5):
        with pytest.raises(ValueError):
            await breaker.call(raises_value_error)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0
