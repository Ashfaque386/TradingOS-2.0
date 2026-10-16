"""The broker-backed market-data provider (real-market-data plan, phase 2).

Fake adapters stand in for the brokers; what is under test is the provider's own rules: history is
pinned to one source per symbol and never blended, only final daily bars are returned, nothing is
fabricated (no broker, an unlisted symbol, a pinned broker that is gone are all errors), and what
brokers cannot supply is left empty rather than invented.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from cryptography.fernet import Fernet

from src.brokers.base import Candle, InstrumentNotFoundError
from src.data import lake
from src.data.broker_provider import (
    BrokerMarketDataProvider,
    NoRealDataSourceError,
    PinnedSourceUnavailableError,
    daily_frame,
    minute_frame,
)
from src.engine.paper_trading.market_hours import IST

# Friday 2026-10-16: after the close (16:30 IST) and during the session (10:30 IST).
AFTER_CLOSE = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
DURING_SESSION = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def _candle(day: date, close: float, *, hour: int = 0, minute: int = 0) -> Candle:
    return Candle(
        timestamp=datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST),
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=100,
    )


class FakeAdapter:
    def __init__(self, name: str, *, lists=("INFY", "TCS"), candles=None):
        self.broker_name = name
        self.lists = set(lists)
        self.candles = candles or [
            _candle(date(2026, 10, 14), 1100.0),
            _candle(date(2026, 10, 15), 1110.0),
            _candle(date(2026, 10, 16), 1120.0),
        ]
        self.history_calls: list[tuple[str, str, date, date]] = []

    async def has_instrument(self, symbol: str) -> bool:
        return symbol.upper() in self.lists

    async def get_historical_candles(self, symbol, interval, start, end):
        self.history_calls.append((symbol, interval, start, end))
        return self.candles


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def _provider(root, adapters, *, now=AFTER_CLOSE) -> BrokerMarketDataProvider:
    return BrokerMarketDataProvider(lambda: adapters, root=root, clock=lambda: now)


def _store_real(root, symbol, source, day="2026-09-01"):
    frame = pd.DataFrame(
        {"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1, "source": source},
        index=pd.DatetimeIndex(pd.to_datetime([day])),
    )
    lake.write_daily_bars(root, symbol, frame)


# ---- choosing the broker ----


async def test_no_connected_broker_is_an_error_never_generated_data(root):
    provider = _provider(root, [])

    with pytest.raises(NoRealDataSourceError, match="no broker is connected"):
        await provider.daily_bars("INFY", date(2026, 10, 14), date(2026, 10, 16))
    with pytest.raises(NoRealDataSourceError):
        await provider.symbol_listed("INFY")


async def test_the_first_connected_broker_that_lists_the_symbol_is_used(root):
    zerodha = FakeAdapter("zerodha", lists=("TCS",))
    upstox = FakeAdapter("upstox", lists=("INFY",))

    adapter = await _provider(root, [zerodha, upstox]).history_adapter("INFY")

    assert adapter is upstox  # zerodha was asked first and does not list it


async def test_the_preferred_broker_wins_when_both_list_the_symbol(root):
    zerodha, upstox = FakeAdapter("zerodha"), FakeAdapter("upstox")

    assert await _provider(root, [zerodha, upstox]).history_adapter("INFY") is zerodha


async def test_a_symbol_pinned_to_a_broker_stays_with_it_even_when_the_other_is_preferred(root):
    _store_real(root, "INFY", "upstox")
    zerodha, upstox = FakeAdapter("zerodha"), FakeAdapter("upstox")

    adapter = await _provider(root, [zerodha, upstox]).history_adapter("INFY")

    assert adapter is upstox


async def test_a_pinned_broker_that_is_not_connected_is_an_error_not_a_quiet_switch(root):
    _store_real(root, "INFY", "upstox")
    provider = _provider(root, [FakeAdapter("zerodha")])  # upstox is gone

    with pytest.raises(PinnedSourceUnavailableError, match="pinned to upstox"):
        await provider.daily_bars("INFY", date(2026, 10, 14), date(2026, 10, 16))


async def test_generated_rows_do_not_pin_a_symbol(root):
    _store_real(root, "INFY", "synthetic")
    zerodha = FakeAdapter("zerodha")

    assert await _provider(root, [zerodha]).history_adapter("INFY") is zerodha


async def test_an_unlisted_symbol_is_an_instrument_error_and_asks_for_no_history(root):
    zerodha = FakeAdapter("zerodha")

    with pytest.raises(InstrumentNotFoundError) as caught:
        await _provider(root, [zerodha]).daily_bars("NOPE", date(2026, 10, 14), date(2026, 10, 16))

    assert caught.value.status_code == 404 and "NOPE" in str(caught.value)
    assert zerodha.history_calls == []


async def test_symbol_listed_asks_each_connected_broker(root):
    provider = _provider(root, [FakeAdapter("zerodha", lists=()), FakeAdapter("upstox")])

    assert await provider.symbol_listed("INFY") is True
    assert await provider.symbol_listed("NOPE") is False


# ---- the bars ----


async def test_daily_bars_are_a_naive_date_indexed_frame_tagged_with_the_broker(root):
    provider = _provider(root, [FakeAdapter("zerodha")])

    frame = await provider.daily_bars("INFY", date(2026, 10, 14), date(2026, 10, 16))

    assert list(frame.columns) == ["open", "high", "low", "close", "volume", "source"]
    assert set(frame["source"]) == {"zerodha"}
    assert frame.index.tz is None
    assert [d.isoformat() for d in frame.index.date] == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert list(frame["close"]) == [1100.0, 1110.0, 1120.0]


async def test_todays_bar_is_withheld_until_the_session_is_over(root):
    adapter = FakeAdapter("zerodha")

    during = await _provider(root, [adapter], now=DURING_SESSION).daily_bars(
        "INFY", date(2026, 10, 14), date(2026, 10, 16)
    )
    after = await _provider(root, [adapter], now=AFTER_CLOSE).daily_bars(
        "INFY", date(2026, 10, 14), date(2026, 10, 16)
    )

    assert [d.isoformat() for d in during.index.date] == ["2026-10-14", "2026-10-15"]
    assert [d.isoformat() for d in after.index.date] == ["2026-10-14", "2026-10-15", "2026-10-16"]


async def test_earlier_days_are_final_whatever_the_time_of_day(root):
    candles = [_candle(date(2026, 10, 14), 1100.0), _candle(date(2026, 10, 15), 1110.0)]
    provider = _provider(root, [FakeAdapter("zerodha", candles=candles)], now=DURING_SESSION)

    frame = await provider.daily_bars("INFY", date(2026, 10, 14), date(2026, 10, 15))

    assert len(frame) == 2


async def test_the_requested_range_and_interval_reach_the_adapter(root):
    adapter = FakeAdapter("zerodha")

    await _provider(root, [adapter]).daily_bars("INFY", date(2026, 1, 1), date(2026, 10, 16))
    await _provider(root, [adapter]).intraday_minute_bars("INFY", date(2026, 10, 16))

    assert adapter.history_calls == [
        ("INFY", "day", date(2026, 1, 1), date(2026, 10, 16)),
        ("INFY", "minute", date(2026, 10, 16), date(2026, 10, 16)),
    ]


def test_minute_candles_become_naive_ist_timestamps():
    frame = minute_frame([_candle(date(2026, 10, 16), 1130.0, hour=9, minute=15)], "upstox")

    assert frame.index[0] == pd.Timestamp("2026-10-16 09:15:00")
    assert frame.index.tz is None and frame["source"].iloc[0] == "upstox"


def test_a_candle_in_another_timezone_lands_on_its_ist_trading_day():
    late_utc = Candle(
        datetime(2026, 10, 15, 19, 0, tzinfo=UTC), 1, 2, 0.5, 1.5, 1
    )  # 00:30 IST 16th

    assert daily_frame([late_utc], "zerodha").index[0] == pd.Timestamp("2026-10-16")


# ---- what brokers cannot supply is left empty, not invented ----


async def test_instrument_master_contains_only_symbols_a_broker_lists(root):
    provider = _provider(root, [FakeAdapter("zerodha", lists=("INFY", "NIFTY", "NIFTY BANK"))])

    records = await provider.instrument_master(["INFY", "NOPE", "NIFTY", "NIFTY BANK"])

    by_symbol = {r.symbol: r for r in records}
    assert set(by_symbol) == {"INFY", "NIFTY", "NIFTY BANK"}  # NOPE is not invented
    assert by_symbol["INFY"].instrument_type == "equity" and by_symbol["INFY"].tick_size == 0.05
    assert by_symbol["NIFTY"].instrument_type == "index"
    assert by_symbol["NIFTY BANK"].instrument_type == "index"
    assert all(r.exchange == "NSE" and r.isin is None for r in records)


async def test_corporate_actions_are_empty_because_brokers_publish_none(root):
    provider = _provider(root, [FakeAdapter("zerodha")])

    assert await provider.corporate_actions("INFY", date(2020, 1, 1)) == []


async def test_the_market_pulse_is_unavailable_rather_than_generated(root):
    with pytest.raises(NoRealDataSourceError, match="not sourced from broker data"):
        await _provider(root, [FakeAdapter("zerodha")]).market_pulse()


# ---- which brokers count as connected ----


def _isolated_store(monkeypatch, tmp_path):
    from src.core.config import get_settings
    from src.security.secrets_store import SecretsStore, get_secrets_store

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "false")
    get_settings.cache_clear()
    get_secrets_store.cache_clear()
    return SecretsStore(tmp_path / "secrets.enc", key)


@pytest.fixture
def _clean_caches():
    from src.core.config import get_settings
    from src.security.secrets_store import get_secrets_store

    yield
    get_settings.cache_clear()
    get_secrets_store.cache_clear()


def test_a_broker_counts_as_connected_only_with_a_live_access_token(
    monkeypatch, tmp_path, _clean_caches
):
    from src.brokers.base import BrokerCredentials
    from src.data.market_data_factory import connected_adapters

    store = _isolated_store(monkeypatch, tmp_path)
    assert connected_adapters() == []  # nothing stored

    store.set_credentials("zerodha", BrokerCredentials(api_key="k"))  # keys, no token
    assert connected_adapters() == []

    store.set_credentials(
        "zerodha",
        BrokerCredentials(
            api_key="k", access_token="t", token_expires_at="2020-01-01T00:00:00+00:00"
        ),
    )  # an expired token
    assert connected_adapters() == []


def test_connected_brokers_come_in_preference_order_behind_their_circuit_breakers(
    monkeypatch, tmp_path, _clean_caches
):
    from src.brokers.base import BrokerCredentials
    from src.brokers.resilient import ResilientBrokerAdapter
    from src.data.market_data_factory import connected_adapters

    store = _isolated_store(monkeypatch, tmp_path)
    store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
    store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))

    adapters = connected_adapters()

    assert [a.broker_name for a in adapters] == ["zerodha", "upstox"]  # not insertion order
    assert all(isinstance(a, ResilientBrokerAdapter) for a in adapters)


def test_the_factory_gives_the_broker_provider_unless_synthetic_mode_is_on(
    monkeypatch, tmp_path, _clean_caches
):
    from src.core.config import get_settings
    from src.data.market_data_factory import get_market_data_provider
    from src.data.providers import FakeMarketDataProvider

    _isolated_store(monkeypatch, tmp_path)
    assert isinstance(get_market_data_provider(), BrokerMarketDataProvider)

    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")
    get_settings.cache_clear()
    assert isinstance(get_market_data_provider(), FakeMarketDataProvider)
