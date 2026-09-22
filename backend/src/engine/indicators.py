"""Technical indicator series (Phase 16 audit follow-up C): pure pandas
functions computing SMA/EMA/RSI/MACD/Bollinger Bands over a close-price
series -- the general-purpose indicator API this codebase never had.
Distinct from `src.engine.backtest.signals`' SMA-crossover logic, which
computes a boolean trade *signal* (long/flat), not a chartable indicator
series.

Every function returns NaN wherever its window hasn't genuinely filled
yet -- no `min_periods=1` partial-window shortcuts. A 20-day SMA reported
before 20 real trading days of history exist would be exactly the kind of
"looks real, quietly wrong" number this codebase refuses to fabricate
elsewhere (Phase 5's null correlations, Phase 7's honest partial fills,
Phase 9's honest `pending_confirmation`).
"""

from __future__ import annotations

import pandas as pd


def simple_moving_average(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window, min_periods=window).mean()


def exponential_moving_average(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, min_periods=span, adjust=False).mean()


def relative_strength_index(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI -- Wilder's smoothing (an EMA with alpha=1/period),
    not the naive simple-moving-average variant some implementations use."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # A zero avg_loss (an unbroken run of gains) makes rs infinite via
    # division -- RSI is genuinely 100 there, not undefined, so this
    # isn't a fabrication, it's the correct closed-form limit.
    return rsi.where(avg_loss != 0, 100.0)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = exponential_moving_average(close, fast)
    ema_slow = exponential_moving_average(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower). `middle` is the same SMA
    `simple_moving_average` would return for this window -- population
    standard deviation (ddof=0), the conventional choice for this
    indicator."""
    middle = simple_moving_average(close, window)
    std = close.rolling(window, min_periods=window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower
