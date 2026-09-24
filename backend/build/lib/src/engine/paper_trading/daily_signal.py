"""Layer 1: Daily Signal Generation (Build Spec §11). Once a day, re-runs
the trusted backtest engine (Phase 5) over fresh daily bars and reads the
position transition on the most recent bar to decide whether today is a
BUY, a SELL, or neither.

`detect_todays_signal` mirrors `src.engine.backtest.engine.
run_vectorized_backtest`'s own one-bar-forward lag
(`position = signals.shift(1)`) exactly, so "today's transition" means
the position the trusted engine would actually hold today given this
signals series -- never a lookahead-biased reading of today's raw signal,
and never a second, independently-written lag rule that could drift from
the engine's.
"""

from dataclasses import dataclass
from typing import Literal

import pandas as pd

SignalType = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class DailySignalDecision:
    signal: SignalType | None
    position_today: int
    position_yesterday: int


def detect_todays_signal(signals: pd.Series) -> DailySignalDecision:
    """`signals` is the raw 0/1 series that would be fed into
    `run_vectorized_backtest` -- long-only, per that engine's own
    convention. Fewer than 2 observations means no transition can be
    detected (never fabricated)."""
    if len(signals) < 2:
        return DailySignalDecision(signal=None, position_today=0, position_yesterday=0)

    position = signals.shift(1).fillna(0).astype(int)
    position_today = int(position.iloc[-1])
    position_yesterday = int(position.iloc[-2])

    if position_yesterday == 0 and position_today == 1:
        signal: SignalType | None = "BUY"
    elif position_yesterday == 1 and position_today == 0:
        signal = "SELL"
    else:
        signal = None

    return DailySignalDecision(
        signal=signal, position_today=position_today, position_yesterday=position_yesterday
    )
