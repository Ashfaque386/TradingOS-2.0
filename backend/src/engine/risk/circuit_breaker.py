"""Broker Circuit Breaker (Build Spec §8): opens after 3 consecutive 5xx
responses from a broker adapter call, then refuses further calls for a
1-minute cooldown before letting a trial call through again. Built here
against a `BrokerAdapter` Protocol and a `FakeBrokerAdapter` test double --
concrete broker adapters (Zerodha/Upstox, Phase 8) only need to raise
`BrokerServerError` for a 5xx for this breaker to work with them unchanged.

The alert fan-out hook (`on_open`) fires synchronously the instant the
breaker opens, carrying a small immutable event -- it is a stub call, not
a real notification. Wiring it to an actual channel (Slack/email/
PagerDuty) is Phase 12's job; passing no `on_open` is a valid, silent
no-op.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class BrokerServerError(Exception):
    """Raised by a broker adapter call for a 5xx-class failure -- the only
    exception type the circuit breaker's failure counter reacts to. Any
    other exception propagates without affecting the breaker's state (a
    4xx/validation error is the caller's mistake, not a broker outage)."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(message or f"broker returned {status_code}")


class CircuitOpenError(Exception):
    def __init__(self, opened_at: float, cooldown_seconds: float):
        self.opened_at = opened_at
        self.cooldown_seconds = cooldown_seconds
        super().__init__(f"circuit is open, cooldown ends at {opened_at + cooldown_seconds:.2f}")


@dataclass(frozen=True, slots=True)
class CircuitOpenedEvent:
    consecutive_failures: int
    opened_at: float
    cooldown_seconds: float


class BrokerAdapter(Protocol):
    async def send_order(self, **kwargs) -> dict: ...


@dataclass
class FakeBrokerAdapter:
    """Test double for Phase 8's not-yet-built concrete broker adapters.
    `remaining_failures` counts down on each call; while positive, the call
    raises `BrokerServerError(503)`, then succeeds once it reaches zero."""

    remaining_failures: int = 0
    calls: int = 0

    async def send_order(self, **kwargs) -> dict:
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise BrokerServerError(503, "simulated broker 5xx")
        return {"status": "ok"}


class BrokerCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        on_open: Callable[[CircuitOpenedEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._on_open = on_open
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def call(self, fn: Callable[[], Awaitable]):
        if self._state == CircuitState.OPEN:
            elapsed = self._clock() - self._opened_at
            if elapsed < self._cooldown_seconds:
                raise CircuitOpenError(self._opened_at, self._cooldown_seconds)
            # Cooldown elapsed: let one trial call through (half-open in
            # all but name) -- a fresh failure below re-opens immediately.
            self._state = CircuitState.CLOSED

        try:
            result = await fn()
        except BrokerServerError:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                if self._on_open is not None:
                    self._on_open(
                        CircuitOpenedEvent(
                            consecutive_failures=self._consecutive_failures,
                            opened_at=self._opened_at,
                            cooldown_seconds=self._cooldown_seconds,
                        )
                    )
            raise
        else:
            self._consecutive_failures = 0
            return result
