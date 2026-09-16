"""WebSocket Latency Guard tests (Build Spec §8): self-clearing, the
opposite of the Kill Switch's latching behavior.
"""

from src.engine.risk.latency_guard import WebSocketLatencyGuard


def test_starts_unpaused():
    guard = WebSocketLatencyGuard(threshold_ms=100.0)
    assert guard.paused is False


def test_pauses_above_threshold():
    guard = WebSocketLatencyGuard(threshold_ms=100.0)
    observation = guard.observe(150.0)
    assert observation.paused is True
    assert guard.paused is True


def test_self_clears_once_latency_recovers():
    guard = WebSocketLatencyGuard(threshold_ms=100.0)
    guard.observe(150.0)
    assert guard.paused is True

    observation = guard.observe(20.0)
    assert observation.paused is False
    assert guard.paused is False


def test_exactly_at_threshold_is_not_paused():
    guard = WebSocketLatencyGuard(threshold_ms=100.0)
    observation = guard.observe(100.0)
    assert observation.paused is False
