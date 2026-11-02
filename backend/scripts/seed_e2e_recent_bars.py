"""E2E test-fixture seeding: a share with recent daily bars in the data lake.

e2e/analysis-pages.spec.ts changes the indicator periods on Market Analysis, which reads the last
250 days of a share's bars up to today. The backtest spec's share (`seed_e2e_lake.py`) has bars from
a fixed first day in 2025, which age out of that window, so this writes 80 weekday bars that end
yesterday: enough for the 20-bar averages and the 26-bar MACD, and too few for a 90-bar one, which
the spec relies on. The symbol is new each run; the bars carry a recorded real source (the lake does
not read bars without one, unless the synthetic demo mode is on).

Usage:
    python scripts/seed_e2e_recent_bars.py
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.core.config import get_settings
from src.data import lake

BARS = 80


def seed() -> None:
    symbol = f"E2EIND{uuid.uuid4().hex[:6].upper()}"
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    days = pd.bdate_range(end=yesterday, periods=BARS)
    rng = np.random.default_rng(5)
    close = 200 * np.cumprod(1 + rng.normal(0.0005, 0.012, BARS))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 40_000,
            "source": "zerodha",
        },
        index=pd.DatetimeIndex(days),
    )
    lake.write_daily_bars(Path(get_settings().data_lake_path), symbol, frame)

    print(f"symbol={symbol}")
    print(f"first={days[0].date().isoformat()}")
    print(f"last={days[-1].date().isoformat()}")
    print(f"bars={BARS}")


if __name__ == "__main__":
    seed()
