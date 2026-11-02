"""E2E test-fixture seeding: a share with a history in the data lake.

e2e/backtests-lake.spec.ts runs a backtest, a walk-forward and an optimisation on prices the data
lake holds, so the lake needs a share with enough of them. This writes 200 weekday bars of a new
symbol each run, from a fixed first day, each with a recorded real source (the lake does not read
bars without one, unless the synthetic demo mode is on). The dates are fixed so a spec can name
the window.

Usage:
    python scripts/seed_e2e_lake.py
"""

import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from src.core.config import get_settings
from src.data import lake

BARS = 200
FIRST_DAY = "2025-01-01"


def seed() -> None:
    suffix = uuid.uuid4().hex[:6].upper()
    symbol = f"E2ELAKE{suffix}"
    days = pd.bdate_range(FIRST_DAY, periods=BARS)
    rng = np.random.default_rng(11)
    close = 100 * np.cumprod(1 + rng.normal(0.0008, 0.015, BARS))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 50_000,
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
