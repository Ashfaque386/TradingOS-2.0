"""WebSocket Latency Guard (Build Spec §8): pauses order-intent generation
while market-data latency is elevated, and resumes on its own the moment
latency recovers -- deliberately the opposite latching behavior from the
Max Drawdown Kill Switch (kill_switch.py). A stale/slow feed is a transient
condition, not a safety breach that needs a human decision to clear, so
this guard's `paused` flag always reflects only the most recent
observation, with no persisted state and no reset function anywhere in
this codebase.
"""

from dataclasses import dataclass

DEFAULT_LATENCY_THRESHOLD_MS = 100.0


@dataclass(frozen=True, slots=True)
class LatencyObservation:
    paused: bool
    latency_ms: float
    threshold_ms: float


class WebSocketLatencyGuard:
    """Per-connection instance -- holds only the latest observed latency
    and its derived pause state, nothing that needs to survive a process
    restart."""

    def __init__(self, *, threshold_ms: float = DEFAULT_LATENCY_THRESHOLD_MS):
        self.threshold_ms = threshold_ms
        self._paused = False
        self._last_latency_ms: float | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def last_latency_ms(self) -> float | None:
        return self._last_latency_ms

    def observe(self, latency_ms: float) -> LatencyObservation:
        """Record one latency sample and recompute `paused` purely from it
        -- a later sample under threshold clears the pause on its own, with
        no separate "resume" call required."""
        self._last_latency_ms = latency_ms
        self._paused = latency_ms > self.threshold_ms
        return LatencyObservation(
            paused=self._paused, latency_ms=latency_ms, threshold_ms=self.threshold_ms
        )
