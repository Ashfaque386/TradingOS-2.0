"""The real `PriceDataProvider` implementation (Build Spec §14) Phase 7's
`src.engine.paper_trading.price_data` module docstring named explicitly:
"Phase 10's real data lake becomes the provider later with no call-site
changes." This is that provider -- `main.py` swaps which instance it
constructs; Layer 1's daily signal generation code is untouched.

Falls back to `FakeDailyPriceProvider` **per symbol** when the lake
doesn't yet have enough ingested history for that symbol to be useful
(freshly enrolled subscription, symbol never ingested) -- the same
graceful-degradation shape as Phase 8's `build_tick_source()` falling back
to `MockTickSource` when no broker is configured, so paper/live trading
never simply breaks for an un-ingested symbol.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

from src.data import lake
from src.engine.paper_trading.price_data import FakeDailyPriceProvider, PriceDataProvider


@dataclass(frozen=True, slots=True)
class DataLakePriceProvider:
    root: Path
    fallback: PriceDataProvider = field(default_factory=FakeDailyPriceProvider)
    # Below this many real rows, the ingested history is too sparse to
    # drive a real signal (e.g. a symbol enrolled today with one day of
    # data) -- fall back rather than run a backtest on near-nothing.
    min_rows_required: int = 5

    def daily_bars(self, symbol: str, *, as_of: pd.Timestamp, lookback_days: int) -> pd.DataFrame:
        as_of_date = as_of.date()
        # Generous calendar-day buffer so weekends/holidays inside the
        # window don't undercount trading days actually present.
        start = as_of_date - timedelta(days=int(lookback_days * 2.2) + 10)
        bars = lake.read_daily_bars(self.root, symbol, start, as_of_date)
        if len(bars) < self.min_rows_required:
            return self.fallback.daily_bars(symbol, as_of=as_of, lookback_days=lookback_days)
        return bars.tail(lookback_days)
