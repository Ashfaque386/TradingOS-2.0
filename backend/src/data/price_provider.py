"""The real `PriceDataProvider` implementation (Build Spec §14) Phase 7's
`src.engine.paper_trading.price_data` module docstring named explicitly:
"Phase 10's real data lake becomes the provider later with no call-site
changes." This is that provider -- `main.py` swaps which instance it
constructs; Layer 1's daily signal generation code is untouched.

When the lake doesn't yet hold enough history for a symbol (a freshly enrolled subscription,
a symbol never ingested) this raises `InsufficientHistoryError`; callers skip that subscription
and report `awaiting_history`. It used to fall back to `FakeDailyPriceProvider` per symbol, which
meant a real symbol's signals came from generated bars -- for live trading too. A fallback is
still accepted, but only when one is passed explicitly (the `PAPER_TRADING_SYNTHETIC` demo/test
mode, see `build_signal_price_provider`); the default is none.
"""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from src.core.config import get_settings
from src.data import lake
from src.engine.paper_trading.price_data import (
    FakeDailyPriceProvider,
    InsufficientHistoryError,
    PriceDataProvider,
)


@dataclass(frozen=True, slots=True)
class DataLakePriceProvider:
    root: Path
    # Only ever set deliberately (synthetic demo/test mode); None = never fabricate bars.
    fallback: PriceDataProvider | None = None
    # Read only rows with a recorded real source: bars written by the generated-data provider (or
    # before sources were recorded) are never allowed to drive a signal.
    real_only: bool = False
    # Below this many real rows, the ingested history is too sparse to
    # drive a real signal (e.g. a symbol enrolled today with one day of
    # data) -- refuse rather than run a backtest on near-nothing.
    min_rows_required: int = 5

    def daily_bars(self, symbol: str, *, as_of: pd.Timestamp, lookback_days: int) -> pd.DataFrame:
        as_of_date = as_of.date()
        # Generous calendar-day buffer so weekends/holidays inside the
        # window don't undercount trading days actually present.
        start = as_of_date - timedelta(days=int(lookback_days * 2.2) + 10)
        bars = lake.read_daily_bars(self.root, symbol, start, as_of_date, real_only=self.real_only)
        if len(bars) < self.min_rows_required:
            if self.fallback is None:
                raise InsufficientHistoryError(symbol, have=len(bars), need=self.min_rows_required)
            return self.fallback.daily_bars(symbol, as_of=as_of, lookback_days=lookback_days)
        return bars.tail(lookback_days)


def build_signal_price_provider(root: Path, *, synthetic: bool) -> DataLakePriceProvider:
    """The one place the daily-signal price provider is built. Real lake history only, unless
    `synthetic` (the `PAPER_TRADING_SYNTHETIC` demo/test mode) asks for generated bars as a
    labelled fallback."""
    return DataLakePriceProvider(
        root=root,
        fallback=FakeDailyPriceProvider() if synthetic else None,
        real_only=not synthetic,
    )


def get_signal_price_provider() -> DataLakePriceProvider:
    """FastAPI dependency for the manual daily-signal endpoints: the same provider the
    schedulers use (real lake history), so a manual run can never be fed generated bars unless
    `PAPER_TRADING_SYNTHETIC` is on. Tests override it."""
    settings = get_settings()
    return build_signal_price_provider(
        Path(settings.data_lake_path), synthetic=settings.paper_trading_synthetic
    )
