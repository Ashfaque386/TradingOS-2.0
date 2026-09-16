"""Built-in signal generators (Build Spec §10/§11): a small, fixed set of
deterministic trading rules -- never arbitrary code -- shared by Phase 5's
backtest API (`src/api/routes/backtests.py`) and Phase 7's autonomous
daily signal layer (`src/orchestration/paper_trading.py`). Originally
private to the Phase 5 route module; promoted here in Phase 7 so both
callers use the exact same function rather than two copies that could
drift apart -- the same "one function, multiple call sites" posture Phase
6 used for its correlation constraint.

`BuiltinStrategy` is the canonical type; `src.api.schemas` re-exports it
for the HTTP layer rather than defining its own copy.
"""

from typing import Literal

import pandas as pd

BuiltinStrategy = Literal["always_long", "sma_crossover"]


def generate_builtin_signals(
    prices: pd.DataFrame, strategy: BuiltinStrategy, sma_window: int
) -> pd.Series:
    if strategy == "always_long":
        return pd.Series(1, index=prices.index)
    sma = prices["close"].rolling(sma_window, min_periods=1).mean()
    return (prices["close"] > sma).astype(int)
