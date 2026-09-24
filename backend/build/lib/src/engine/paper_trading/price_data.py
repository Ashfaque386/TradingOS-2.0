"""Daily OHLCV price data for Layer 1's once-a-day backtest re-run (Build
Spec §11). `PriceDataProvider` is a `Protocol` -- the same swap-later
posture as Phase 5's `DataLakeFreshnessCheck` and Phase 6's
`RegulatoryDataProvider` -- so Phase 10's real data lake becomes the
provider later with no call-site changes. `FakeDailyPriceProvider` is a
deterministic, seeded stand-in used until then.
"""

import zlib
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd


def _stable_seed_for(symbol: str) -> int:
    """A deterministic per-symbol seed. Deliberately NOT Python's builtin
    `hash()`: str hashing is salted per-process by default (`PYTHONHASHSEED`
    is random unless explicitly pinned), so `hash(symbol)` gives a
    different value every process run -- silently defeating the whole
    point of a "deterministic, seeded" fake provider. crc32 over the UTF-8
    bytes is stable across processes and interpreters."""
    return zlib.crc32(symbol.encode("utf-8")) % 1000


# A single continuous synthetic price path per symbol, generated once and
# sliced per `as_of` -- NOT regenerated independently per call. Regenerating
# from `as_of` backwards on every call would make each day's lookback window
# an unrelated random draw with no relationship to the previous day's, which
# breaks any day-over-day signal (every "transition" would be an artifact of
# resampling, not a real price move). A ~16-year fixed window comfortably
# covers any `as_of` a caller is likely to pass.
_SERIES_START = pd.Timestamp("2020-01-01")
_SERIES_END = pd.Timestamp("2035-12-31")


class PriceDataProvider(Protocol):
    def daily_bars(self, symbol: str, *, as_of: pd.Timestamp, lookback_days: int) -> pd.DataFrame:
        """Returns an OHLCV DataFrame (columns open/high/low/close/volume,
        DatetimeIndex) covering the `lookback_days` trading days up to and
        including `as_of`."""
        ...


@dataclass(frozen=True, slots=True)
class FakeDailyPriceProvider:
    """Deterministic synthetic daily bars, seeded per symbol so repeated
    calls for the same symbol return consistent, overlapping history
    regardless of `as_of` -- the real feed is a Phase 10 data-ingestion
    concern."""

    seed_by_symbol: dict[str, int] = field(default_factory=dict)
    default_seed: int = 0
    daily_mean: float = 0.0005
    daily_vol: float = 0.012
    start_price: float = 100.0
    _series_cache: dict[str, pd.DataFrame] = field(default_factory=dict, init=False, repr=False)

    def _full_series(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._series_cache:
            seed = self.seed_by_symbol.get(symbol, self.default_seed + _stable_seed_for(symbol))
            rng = np.random.default_rng(seed)
            idx = pd.bdate_range(start=_SERIES_START, end=_SERIES_END)
            returns = rng.normal(self.daily_mean, self.daily_vol, len(idx))
            close = self.start_price * np.cumprod(1 + returns)
            self._series_cache[symbol] = pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": rng.integers(10_000, 50_000, len(idx)),
                },
                index=idx,
            )
        return self._series_cache[symbol]

    def daily_bars(self, symbol: str, *, as_of: pd.Timestamp, lookback_days: int) -> pd.DataFrame:
        return self._full_series(symbol).loc[:as_of].tail(lookback_days)
