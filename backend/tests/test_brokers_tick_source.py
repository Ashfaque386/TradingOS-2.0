"""Broker-quote tick source tests (Build Spec §13): BrokerQuoteTickSource
turns a live BrokerAdapter.get_quote() call into a Tick, and
build_tick_source() picks it over the Phase 7 MockTickSource fallback
exactly when credentials are configured -- proving the swap is real, not
just that each piece works alone.
"""

import httpx
from cryptography.fernet import Fernet

from src.brokers.base import BrokerCredentials
from src.brokers.tick_source import (
    BrokerQuoteTickSource,
    _RefreshingTickSource,
    build_tick_source,
    is_broker_configured,
)
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.core.config import get_settings
from src.engine.paper_trading.tick_feed import MockTickSource
from src.security.secrets_store import SecretsStore, get_secrets_store


def _reset_settings_and_store_caches() -> None:
    get_settings.cache_clear()
    get_secrets_store.cache_clear()


async def test_broker_quote_tick_source_returns_a_tick_from_the_adapters_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"INFY": {"last_price": 1503.25, "depth": {"buy": [], "sell": []}}}},
        )

    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    source = BrokerQuoteTickSource(adapter)

    tick = await source.next_tick("INFY")
    assert tick.symbol == "INFY"
    assert tick.price == 1503.25
    assert tick.timestamp_ms > 0


def test_build_tick_source_falls_back_to_mock_when_no_key_configured(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_settings_and_store_caches()
    try:
        source = build_tick_source()
        assert isinstance(source, MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_build_tick_source_falls_back_to_mock_when_no_credentials_stored(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    _reset_settings_and_store_caches()
    try:
        source = build_tick_source()
        assert isinstance(source, MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_build_tick_source_uses_broker_quotes_when_credentials_configured(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        source = build_tick_source()
        assert isinstance(source, BrokerQuoteTickSource)
        assert source.adapter.broker_name == "zerodha"
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_false_with_no_encryption_key(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_settings_and_store_caches()
    try:
        assert is_broker_configured() is False
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_false_with_an_empty_store(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    _reset_settings_and_store_caches()
    try:
        assert is_broker_configured() is False
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_true_once_a_broker_has_real_credentials(tmp_path, monkeypatch):
    """Same real credential lookup `build_tick_source()` uses -- proving
    `is_broker_configured()` agrees with which `TickSource` is actually
    running, not an independently-drifting check."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        assert is_broker_configured() is True
        assert isinstance(build_tick_source(), BrokerQuoteTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_reuses_the_same_instance_when_config_is_unchanged(
    tmp_path, monkeypatch
):
    """The real bug this fix closes was two-sided: rebuilding on every
    call would silently reset MockTickSource's per-symbol random walk
    every tick-publish firing. Proves the cached instance survives
    repeated calls when nothing about the broker config changed."""
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_settings_and_store_caches()
    try:
        refreshing = _RefreshingTickSource()
        first = refreshing()
        second = refreshing()
        assert first is second
        assert isinstance(first, MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_rebuilds_the_moment_a_broker_is_connected(tmp_path, monkeypatch):
    """The actual Phase 17 Part 4 bug: a broker connected via Settings
    used to only take effect after a backend restart, since
    build_tick_source() was resolved once at process startup. Proves the
    next call after credentials are saved picks it up immediately."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, MockTickSource)

        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, BrokerQuoteTickSource)
        assert after.adapter.broker_name == "zerodha"
        assert after is not before
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_rebuilds_when_the_same_brokers_credentials_change(
    tmp_path, monkeypatch
):
    """Re-saving a corrected key/token for an already-configured broker
    must also refresh -- the broker-name-only half of the fingerprint
    wouldn't catch this, since "zerodha" stays "zerodha" either way."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="old-key", access_token="t"))
        get_secrets_store.cache_clear()

        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, BrokerQuoteTickSource)

        store.set_credentials("zerodha", BrokerCredentials(api_key="new-key", access_token="t2"))
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, BrokerQuoteTickSource)
        assert after is not before
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_falls_back_to_mock_once_a_broker_is_disconnected(
    tmp_path, monkeypatch
):
    """The symmetric case: disconnecting a broker must also take effect
    on the next call, not just connecting one."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, BrokerQuoteTickSource)

        store.delete_credentials("zerodha")
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, MockTickSource)
    finally:
        _reset_settings_and_store_caches()
