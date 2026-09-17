"""Phase 10's real PriceDataProvider implementation (Build Spec §14):
reads real ingested daily bars when there are enough of them, and falls
back to the synthetic FakeDailyPriceProvider per-symbol when the lake
doesn't have enough history yet -- the same graceful-degradation shape as
Phase 8's build_tick_source().
"""

from pathlib import Path

import pandas as pd
import pytest

from src.data import lake
from src.data.price_provider import DataLakePriceProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider


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


def test_falls_back_to_synthetic_when_lake_has_too_little_history(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars(2, "2026-09-14"))  # below min_rows_required
    fallback = FakeDailyPriceProvider()
    provider = DataLakePriceProvider(root=lake_root, fallback=fallback, min_rows_required=5)

    result = provider.daily_bars("DEMOSTOCK", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)
    expected = fallback.daily_bars("DEMOSTOCK", as_of=pd.Timestamp("2026-09-16"), lookback_days=10)
    pd.testing.assert_frame_equal(result, expected)


def test_falls_back_for_never_ingested_symbol(lake_root: Path):
    fallback = FakeDailyPriceProvider()
    provider = DataLakePriceProvider(root=lake_root, fallback=fallback)

    result = provider.daily_bars(
        "NEVERINGESTED", as_of=pd.Timestamp("2026-09-16"), lookback_days=10
    )
    assert len(result) == 10
