"""Phase 10's real PriceDataProvider implementation (Build Spec §14):
reads real ingested daily bars when there are enough of them. When there are not, it **refuses**
(`InsufficientHistoryError`) -- it no longer substitutes the synthetic FakeDailyPriceProvider,
which meant a real symbol's signals (live trading's too) came from generated bars. A fallback
exists only when one is passed explicitly (the PAPER_TRADING_SYNTHETIC demo/test mode).
"""

from pathlib import Path

import pandas as pd
import pytest

from src.core.config import get_settings
from src.data import lake
from src.data.price_provider import (
    DataLakePriceProvider,
    build_signal_price_provider,
    get_signal_price_provider,
)
from src.engine.paper_trading.price_data import FakeDailyPriceProvider, InsufficientHistoryError


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def _bars(n: int, start: str) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0 + i for i in range(n)],
            "volume": [1000] * n,
        },
        index=idx,
    )


def test_uses_real_lake_data_when_enough_history_ingested(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars(30, "2026-08-01"))
    provider = DataLakePriceProvider(root=lake_root, fallback=FakeDailyPriceProvider())

    result = provider.daily_bars("DEMOSTOCK", as_of=pd.Timestamp("2026-09-10"), lookback_days=10)
    assert len(result) == 10
    # Real ingested closes are 100.0, 101.0, 102.0, ... -- the fallback's
    # synthetic random walk would never produce this exact sequence.
    assert list(result["close"])[-1] > 100.0


def test_too_little_history_is_refused_not_filled_with_generated_bars(lake_root: Path):
    lake.write_daily_bars(lake_root, "RELIANCE", _bars(2, "2026-09-14"))
    provider = DataLakePriceProvider(root=lake_root, min_rows_required=5)

    with pytest.raises(InsufficientHistoryError) as caught:
        provider.daily_bars("RELIANCE", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)

    assert (caught.value.symbol, caught.value.have, caught.value.need) == ("RELIANCE", 2, 5)
    assert "history has not been ingested yet" in str(caught.value)


def test_a_never_ingested_symbol_is_refused_by_default(lake_root: Path):
    provider = DataLakePriceProvider(root=lake_root)

    with pytest.raises(InsufficientHistoryError) as caught:
        provider.daily_bars("NEVERINGESTED", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)

    assert caught.value.have == 0


def test_exactly_the_minimum_is_enough_and_one_fewer_is_not(lake_root: Path):
    lake.write_daily_bars(lake_root, "EDGE", _bars(5, "2026-09-07"))
    provider = DataLakePriceProvider(root=lake_root, min_rows_required=5)
    assert len(provider.daily_bars("EDGE", as_of=pd.Timestamp("2026-09-11"), lookback_days=10)) == 5

    short = DataLakePriceProvider(root=lake_root, min_rows_required=6)
    with pytest.raises(InsufficientHistoryError):
        short.daily_bars("EDGE", as_of=pd.Timestamp("2026-09-11"), lookback_days=10)


def test_a_fallback_passed_explicitly_still_works_for_a_short_history(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars(2, "2026-09-14"))  # below min_rows_required
    fallback = FakeDailyPriceProvider()
    provider = DataLakePriceProvider(root=lake_root, fallback=fallback, min_rows_required=5)

    result = provider.daily_bars("DEMOSTOCK", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)
    expected = fallback.daily_bars("DEMOSTOCK", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)
    pd.testing.assert_frame_equal(result, expected)


def test_an_explicit_fallback_still_covers_a_never_ingested_symbol(lake_root: Path):
    fallback = FakeDailyPriceProvider()
    provider = DataLakePriceProvider(root=lake_root, fallback=fallback)

    result = provider.daily_bars(
        "NEVERINGESTED", as_of=pd.Timestamp("2026-09-16"), lookback_days=10
    )
    assert len(result) == 10


# ---- the guard: nothing in the default wiring can reach generated bars ----


def test_the_default_signal_provider_has_no_synthetic_fallback(lake_root: Path):
    assert build_signal_price_provider(lake_root, synthetic=False).fallback is None


def test_synthetic_mode_adds_the_generated_fallback_and_only_then(lake_root: Path):
    provider = build_signal_price_provider(lake_root, synthetic=True)

    assert isinstance(provider.fallback, FakeDailyPriceProvider)


def test_the_dependency_follows_the_setting(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path / "lake"))
    try:
        monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "false")
        get_settings.cache_clear()
        real = get_signal_price_provider()
        assert real.fallback is None and real.root == tmp_path / "lake"

        monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")
        get_settings.cache_clear()
        assert isinstance(get_signal_price_provider().fallback, FakeDailyPriceProvider)
    finally:
        get_settings.cache_clear()
