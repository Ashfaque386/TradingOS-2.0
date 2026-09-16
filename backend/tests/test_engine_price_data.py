"""Fake daily price provider tests (Build Spec §11): the series must be a
single continuous path per symbol, not independently resampled per call --
otherwise no day-over-day signal would ever be meaningful.
"""

import pandas as pd

from src.engine.paper_trading.price_data import FakeDailyPriceProvider


def test_returns_the_requested_lookback_length():
    provider = FakeDailyPriceProvider()
    bars = provider.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=60)
    assert len(bars) == 60
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]


def test_repeated_calls_are_reproducible():
    provider = FakeDailyPriceProvider()
    b1 = provider.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=60)
    b2 = provider.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=60)
    assert b1.equals(b2)


def test_overlapping_windows_share_the_same_history():
    provider = FakeDailyPriceProvider()
    today = provider.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=60)
    yesterday = provider.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-14"), lookback_days=60)
    assert today["close"].iloc[-2] == yesterday["close"].iloc[-1]


def test_different_symbols_get_different_series():
    provider = FakeDailyPriceProvider()
    a = provider.daily_bars("AAA", as_of=pd.Timestamp("2026-09-15"), lookback_days=10)
    b = provider.daily_bars("BBB", as_of=pd.Timestamp("2026-09-15"), lookback_days=10)
    assert not a["close"].equals(b["close"])


def test_explicit_seed_override_is_reproducible_across_instances():
    p1 = FakeDailyPriceProvider(seed_by_symbol={"DEMO": 42})
    p2 = FakeDailyPriceProvider(seed_by_symbol={"DEMO": 42})
    b1 = p1.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=10)
    b2 = p2.daily_bars("DEMO", as_of=pd.Timestamp("2026-09-15"), lookback_days=10)
    assert b1.equals(b2)
