"""Per-broker BrokerCircuitBreaker singleton registry tests (Phase 16
audit follow-up A) -- src.brokers.breaker_registry.
"""

from src.brokers import breaker_registry
from src.engine.risk.circuit_breaker import CircuitState


def test_get_broker_circuit_breaker_returns_the_same_instance_for_the_same_broker():
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        first = breaker_registry.get_broker_circuit_breaker("zerodha")
        second = breaker_registry.get_broker_circuit_breaker("zerodha")
        assert first is second
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)


def test_different_broker_names_get_different_instances():
    breaker_registry._BREAKERS.pop("zerodha", None)
    breaker_registry._BREAKERS.pop("upstox", None)
    try:
        zerodha = breaker_registry.get_broker_circuit_breaker("zerodha")
        upstox = breaker_registry.get_broker_circuit_breaker("upstox")
        assert zerodha is not upstox
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
        breaker_registry._BREAKERS.pop("upstox", None)


def test_get_all_broker_circuit_breakers_does_not_eagerly_create_entries():
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        assert "zerodha" not in breaker_registry.get_all_broker_circuit_breakers()
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)


def test_get_all_broker_circuit_breakers_reflects_a_real_created_instance():
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        created = breaker_registry.get_broker_circuit_breaker("zerodha")
        snapshot = breaker_registry.get_all_broker_circuit_breakers()
        assert snapshot["zerodha"] is created
        assert snapshot["zerodha"].state == CircuitState.CLOSED
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
