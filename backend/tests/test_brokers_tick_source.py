"""Broker-quote tick source tests (Build Spec §13): BrokerQuoteTickSource
turns a live BrokerAdapter.get_quote() call into a Tick, and
build_tick_source() picks it over the Phase 7 MockTickSource fallback
exactly when credentials are configured -- proving the swap is real, not
just that each piece works alone.
"""

import httpx
from cryptography.fernet import Fernet

from src.brokers.base import BrokerCredentials
from src.brokers.tick_source import BrokerQuoteTickSource, build_tick_source
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
