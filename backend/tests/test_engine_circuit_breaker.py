"""Broker Circuit Breaker tests (Build Spec §8): opens after 3 consecutive
5xx responses, refuses calls during a 1-minute cooldown, and lets a trial
call through once the cooldown elapses -- exercised against a fake broker
adapter and an injectable clock (no real sleeping).
"""

from dataclasses import FrozenInstanceError

import pytest

from src.engine.risk.circuit_breaker import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    BrokerCircuitBreaker,
    BrokerServerError,
    CircuitOpenedEvent,
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


async def test_cooldown_remaining_seconds_is_none_while_closed():
    clock, _advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    assert breaker.cooldown_remaining_seconds() is None


async def test_cooldown_remaining_seconds_counts_down_and_clamps_at_zero_once_open():
    clock, advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=3)

    for _ in range(3):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)
    assert breaker.cooldown_remaining_seconds() == 60.0

    advance(45.0)
    assert breaker.cooldown_remaining_seconds() == 15.0

    advance(1000.0)  # well past the cooldown -- never goes negative
    assert breaker.cooldown_remaining_seconds() == 0.0


# ---- Found by the mutation gate (2026-10-14) ------------------------------------
# The defaults, the exact cooldown boundary, the accessors the status API reads, the
# error types' attributes and the in-module test double were all unchecked.


def test_the_documented_defaults_are_three_failures_and_a_one_minute_cooldown():
    breaker = BrokerCircuitBreaker()

    assert (DEFAULT_FAILURE_THRESHOLD, DEFAULT_COOLDOWN_SECONDS) == (3, 60.0)
    assert breaker.failure_threshold == 3
    assert breaker.cooldown_seconds == 60.0


async def test_a_default_breaker_opens_on_exactly_the_third_failure():
    breaker = BrokerCircuitBreaker()
    adapter = FakeBrokerAdapter(remaining_failures=10)

    for expected_open in (False, False, True):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)
        assert (breaker.state == CircuitState.OPEN) is expected_open


async def test_the_cooldown_boundary_is_exact():
    clock, advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=1)
    with pytest.raises(BrokerServerError):
        await breaker.call(adapter.send_order)  # opens at t=0

    advance(59.999)
    with pytest.raises(CircuitOpenError):
        await breaker.call(adapter.send_order)  # still refused just before the boundary
    advance(0.001)  # exactly 60.0 elapsed
    assert await breaker.call(adapter.send_order) == {"status": "ok"}  # the trial goes through
    assert breaker.state == CircuitState.CLOSED


async def test_the_accessors_report_the_real_state():
    clock, advance = _fake_clock()
    advance(100.0)
    breaker = BrokerCircuitBreaker(failure_threshold=2, cooldown_seconds=30.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=2)
    assert breaker.opened_at is None and breaker.consecutive_failures == 0

    for _ in range(2):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    assert breaker.state == CircuitState.OPEN and breaker.consecutive_failures == 2
    assert breaker.opened_at == 100.0
    assert (breaker.failure_threshold, breaker.cooldown_seconds) == (2, 30.0)


async def test_the_remaining_cooldown_is_none_again_once_the_circuit_has_recovered():
    clock, advance = _fake_clock()
    breaker = BrokerCircuitBreaker(failure_threshold=1, cooldown_seconds=10.0, clock=clock)
    adapter = FakeBrokerAdapter(remaining_failures=1)
    with pytest.raises(BrokerServerError):
        await breaker.call(adapter.send_order)
    advance(4.0)
    assert breaker.cooldown_remaining_seconds() == 6.0  # a real countdown, not elapsed + opened_at

    advance(7.0)
    await breaker.call(adapter.send_order)  # the trial succeeds and closes it

    # `opened_at` is still set from the earlier outage, but nothing is counting down.
    assert breaker.state == CircuitState.CLOSED
    assert breaker.cooldown_remaining_seconds() is None


async def test_the_countdown_uses_time_since_it_opened_not_the_absolute_clock():
    clock, advance = _fake_clock()
    advance(1000.0)
    breaker = BrokerCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
    with pytest.raises(BrokerServerError):
        await breaker.call(FakeBrokerAdapter(remaining_failures=1).send_order)
    advance(15.0)

    assert breaker.cooldown_remaining_seconds() == 45.0
    with pytest.raises(CircuitOpenError):
        await breaker.call(FakeBrokerAdapter().send_order)  # 15 s into a 60 s cooldown


def test_the_state_values_are_the_strings_the_status_api_reports():
    assert CircuitState.CLOSED == "closed" and CircuitState.OPEN == "open"
    assert CircuitState.CLOSED.value == "closed" and CircuitState.OPEN.value == "open"


def test_a_server_error_carries_its_status_and_a_default_message():
    assert BrokerServerError(502).status_code == 502
    assert str(BrokerServerError(502)) == "broker returned 502"
    assert str(BrokerServerError(502, "gateway timeout")) == "gateway timeout"


def test_the_open_error_says_when_the_cooldown_ends():
    error = CircuitOpenError(opened_at=100.0, cooldown_seconds=60.0)

    assert (error.opened_at, error.cooldown_seconds) == (100.0, 60.0)
    assert str(error) == "circuit is open, cooldown ends at 160.00"


def test_the_opened_event_is_immutable():
    event = CircuitOpenedEvent(consecutive_failures=3, opened_at=1.0, cooldown_seconds=60.0)

    with pytest.raises(FrozenInstanceError):
        event.consecutive_failures = 0  # type: ignore[misc]


async def test_the_open_hook_receives_the_real_numbers():
    clock, advance = _fake_clock()
    advance(5.0)
    events = []
    breaker = BrokerCircuitBreaker(
        failure_threshold=2, cooldown_seconds=45.0, on_open=events.append, clock=clock
    )
    adapter = FakeBrokerAdapter(remaining_failures=2)

    for _ in range(2):
        with pytest.raises(BrokerServerError):
            await breaker.call(adapter.send_order)

    assert events == [
        CircuitOpenedEvent(consecutive_failures=2, opened_at=5.0, cooldown_seconds=45.0)
    ]


async def test_the_fake_adapter_counts_calls_and_fails_the_requested_number_of_times():
    adapter = FakeBrokerAdapter()
    assert (adapter.remaining_failures, adapter.calls) == (0, 0)
    assert await adapter.send_order() == {"status": "ok"} and adapter.calls == 1

    flaky = FakeBrokerAdapter(remaining_failures=2)
    for attempt in (1, 2):
        with pytest.raises(BrokerServerError) as caught:
            await flaky.send_order()
        assert caught.value.status_code == 503 and str(caught.value) == "simulated broker 5xx"
        assert (flaky.calls, flaky.remaining_failures) == (attempt, 2 - attempt)
    assert await flaky.send_order() == {"status": "ok"}
    assert (flaky.calls, flaky.remaining_failures) == (3, 0)
