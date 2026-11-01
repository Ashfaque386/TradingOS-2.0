"""build_configured_adapter tests (Phase 16 audit follow-up A): the real
gap was that every call built a fresh, zero-state `BrokerCircuitBreaker`
-- so consecutive failures across different real callers (tick source,
live-trading scheduler, per-request API adapters) could never actually
trip it, and there was no persistent state to query even if it had.
These tests prove the fix: repeated calls for the same broker now share
one `BrokerCircuitBreaker` instance (src.brokers.breaker_registry).
"""

from cryptography.fernet import Fernet

from src.brokers import breaker_registry
from src.brokers.base import BrokerCredentials
from src.brokers.factory import RefreshingConfiguredAdapter, build_configured_adapter
from src.core.config import get_settings
from src.security.secrets_store import SecretsStore, get_secrets_store


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_secrets_store.cache_clear()


def test_returns_none_with_no_credentials_configured(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_caches()
    try:
        assert build_configured_adapter() is None
    finally:
        _reset_caches()


def test_repeated_calls_share_the_same_persistent_breaker_instance(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_caches()
    breaker_registry._BREAKERS.pop("zerodha", None)
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        first = build_configured_adapter()
        second = build_configured_adapter()

        assert first is not None and second is not None
        assert first is not second  # a fresh ResilientBrokerAdapter each call ...
        assert first.breaker is second.breaker  # ... but the same breaker underneath
        assert first.breaker is breaker_registry.get_broker_circuit_breaker("zerodha")
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
        _reset_caches()


def test_different_brokers_get_independent_breaker_instances(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_caches()
    breaker_registry._BREAKERS.pop("zerodha", None)
    breaker_registry._BREAKERS.pop("upstox", None)
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        adapter = build_configured_adapter()
        assert adapter is not None
        assert adapter.broker_name == "upstox"
        assert adapter.breaker is breaker_registry.get_broker_circuit_breaker("upstox")
        assert adapter.breaker is not breaker_registry.get_broker_circuit_breaker("zerodha")
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
        breaker_registry._BREAKERS.pop("upstox", None)
        _reset_caches()


# ---- an adapter that follows the stored credentials ----


def _store_with(tmp_path, monkeypatch, **credentials_by_broker):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_caches()
    store = SecretsStore(store_path, key)
    for broker, creds in credentials_by_broker.items():
        store.set_credentials(broker, creds)
    get_secrets_store.cache_clear()
    return store


def test_the_refreshing_adapter_builds_once_and_reuses_it_while_nothing_changes(
    tmp_path, monkeypatch
):
    _store_with(tmp_path, monkeypatch, zerodha=BrokerCredentials(api_key="k", access_token="t"))
    try:
        source = RefreshingConfiguredAdapter()

        first, second, third = source(), source(), source()

        assert first is not None
        assert first is second is third
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
        _reset_caches()


def test_the_refreshing_adapter_is_rebuilt_when_the_token_is_replaced(tmp_path, monkeypatch):
    store = _store_with(
        tmp_path, monkeypatch, zerodha=BrokerCredentials(api_key="k", access_token="t1")
    )
    try:
        source = RefreshingConfiguredAdapter()
        before = source()

        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t2"))
        after = source()

        assert after is not before  # the morning's re-authentication takes effect on the next call
        assert after.breaker is before.breaker  # without losing the broker's breaker state
        assert source() is after
    finally:
        breaker_registry._BREAKERS.pop("zerodha", None)
        _reset_caches()


def test_the_refreshing_adapter_notices_a_broker_being_connected_and_removed(tmp_path, monkeypatch):
    store = _store_with(tmp_path, monkeypatch)
    try:
        source = RefreshingConfiguredAdapter()
        assert source() is None  # nothing configured yet: remembered, not rebuilt every call

        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        connected = source()
        assert connected is not None and connected.broker_name == "upstox"

        store.delete_credentials("upstox")
        assert source() is None
    finally:
        breaker_registry._BREAKERS.pop("upstox", None)
        _reset_caches()


def test_a_missing_secrets_key_means_no_adapter_not_a_crash(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_caches()
    try:
        assert RefreshingConfiguredAdapter()() is None
    finally:
        _reset_caches()


def test_the_live_jobs_use_a_production_adapter_source():
    from src.brokers.factory import get_live_adapter

    assert isinstance(get_live_adapter, RefreshingConfiguredAdapter)
    assert get_live_adapter._sandbox is False
