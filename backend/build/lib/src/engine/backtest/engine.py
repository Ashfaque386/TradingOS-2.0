"""Vectorized single-symbol backtesting engine (Build Spec §10).

Given OHLCV price data and a long/flat position-signal series, computes
trade-level P&L with the real Indian cost model (`friction.py`) applied
per leg, then a bar-by-bar equity curve and summary metrics built with
`numpy`/`pandas` vector operations -- not a Python loop over every bar.
The one place this engine loops in Python is over *trades* (entry/exit
pairs), not bars: position sizing compounds capital trade-to-trade, which
is inherently sequential (trade N+1's size depends on trade N's outcome),
the same reason every vectorized backtesting library still resolves
position sizing this way internally. Trades are typically orders of
magnitude fewer than bars, so this stays fast; the equity curve, returns,
and every metric derived from them are pure vector operations.

**Never fabricates a metric**: every value in `BacktestMetrics` is `None`,
not `0` or another default, whenever the underlying computation is
undefined or non-finite (e.g. Sharpe with zero-variance returns, CAGR
over a non-positive time span, profit factor with no losing trades to
divide by). Returning `0.0` for "Sharpe couldn't be computed" would read
as "this strategy has zero risk-adjusted return", which is a different
and false claim.

Signals are long-only (`1` = long, `0` = flat) -- short-selling carries
materially different Indian regulatory costs (STT treatment, uncovered
short delivery restrictions) that are out of scope here. A signal is
acted on at the *next* bar's open (never the same bar it was computed on)
to avoid lookahead bias.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.engine.backtest.friction import (
    FrictionModel,
    compute_leg_costs,
    compute_slippage_per_share,
)

TRADING_DAYS_PER_YEAR = 252
ATR_PERIOD = 14
VOLUME_LOOKBACK = 20


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _compute_atr(prices: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = prices["high"], prices["low"], prices["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()


@dataclass(frozen=True, slots=True)
class Trade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    costs: float
    net_pnl: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: float | None
    cagr: float | None
    sharpe: float | None
    max_drawdown: float | None
    win_rate: float | None
    profit_factor: float | None
    num_trades: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    daily_returns: pd.Series
    metrics: BacktestMetrics
    friction_model: FrictionModel = field(repr=False, default_factory=FrictionModel)


def _build_trades(
    prices: pd.DataFrame,
    position: pd.Series,
    *,
    initial_capital: float,
    is_delivery: bool,
    friction_model: FrictionModel,
    atr: pd.Series,
    avg_volume: pd.Series,
) -> tuple[list[Trade], float]:
    opens = prices["open"].to_numpy()
    closes = prices["close"].to_numpy()
    index = prices.index
    pos = position.to_numpy()

    change = np.diff(np.concatenate(([0], pos)))
    entry_positions = np.flatnonzero(change == 1)
    exit_positions = np.flatnonzero(change == -1)

    # A position still open at the end of the window is closed at the
    # final bar's close (mark-to-market exit) rather than left dangling --
    # every entry gets a corresponding exit.
    if len(entry_positions) > len(exit_positions):
        exit_positions = np.append(exit_positions, len(pos) - 1)

    trades: list[Trade] = []
    capital = initial_capital

    for entry_idx, exit_idx in zip(entry_positions, exit_positions, strict=True):
        raw_entry_price = opens[entry_idx]
        raw_exit_price = opens[exit_idx] if exit_idx < len(pos) - 1 else closes[exit_idx]

        slip_entry = compute_slippage_per_share(
            atr=atr.iloc[entry_idx],
            quantity=1,
            avg_daily_volume=avg_volume.iloc[entry_idx],
            price=raw_entry_price,
            model=friction_model,
        )
        entry_price = raw_entry_price + slip_entry  # buying costs more

        quantity = int(capital // entry_price) if entry_price > 0 else 0
        if quantity <= 0:
            continue

        slip_exit = compute_slippage_per_share(
            atr=atr.iloc[exit_idx],
            quantity=quantity,
            avg_daily_volume=avg_volume.iloc[exit_idx],
            price=raw_exit_price,
            model=friction_model,
        )
        exit_price = max(raw_exit_price - slip_exit, 0.0)  # selling nets less

        entry_costs = compute_leg_costs(
            turnover=quantity * entry_price,
            is_buy=True,
            is_delivery=is_delivery,
            model=friction_model,
        )
        exit_costs = compute_leg_costs(
            turnover=quantity * exit_price,
            is_buy=False,
            is_delivery=is_delivery,
            model=friction_model,
        )

        gross_pnl = quantity * (exit_price - entry_price)
        total_costs = entry_costs.total + exit_costs.total
        net_pnl = gross_pnl - total_costs
        capital += net_pnl

        trades.append(
            Trade(
                entry_date=index[entry_idx],
                exit_date=index[exit_idx],
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                gross_pnl=gross_pnl,
                costs=total_costs,
                net_pnl=net_pnl,
            )
        )

    return trades, capital


def _build_equity_curve(
    prices: pd.DataFrame, trades: list[Trade], *, initial_capital: float
) -> pd.Series:
    index = prices.index
    closes = prices["close"]
    shares_held = pd.Series(0, index=index, dtype=float)
    cash = pd.Series(initial_capital, index=index, dtype=float)

    for trade in trades:
        shares_held.loc[trade.entry_date : trade.exit_date] = trade.quantity
        # Cash leaves at entry (the gross buy cost) and returns at exit
        # (the gross sell proceeds, less this round trip's total costs) --
        # the two together net out to exactly trade.net_pnl over the
        # position's lifetime, with no double-counting of costs.
        cash.loc[trade.entry_date :] -= trade.quantity * trade.entry_price
        cash.loc[trade.exit_date :] += trade.quantity * trade.exit_price - trade.costs

    equity = cash + shares_held * closes
    return equity


def run_vectorized_backtest(
    prices: pd.DataFrame,
    signals: pd.Series,
    *,
    initial_capital: float = 100_000.0,
    is_delivery: bool = True,
    friction_model: FrictionModel = FrictionModel(),
) -> BacktestResult:
    """`prices` needs a `DatetimeIndex` and open/high/low/close/volume
    columns; `signals` must be aligned to `prices.index` with values in
    `{0, 1}`. Signals are shifted one bar forward before use, so a signal
    computed from bar *t*'s close is acted on at bar *t+1*'s open.
    """
    aligned_signals = signals.reindex(prices.index).fillna(0)
    position = aligned_signals.shift(1).fillna(0).astype(int)

    atr = _compute_atr(prices)
    avg_volume = prices["volume"].rolling(VOLUME_LOOKBACK, min_periods=1).mean()

    trades, _final_capital = _build_trades(
        prices,
        position,
        initial_capital=initial_capital,
        is_delivery=is_delivery,
        friction_model=friction_model,
        atr=atr,
        avg_volume=avg_volume,
    )

    equity_curve = _build_equity_curve(prices, trades, initial_capital=initial_capital)
    daily_returns = equity_curve.pct_change().dropna()

    metrics = _compute_metrics(
        equity_curve=equity_curve,
        daily_returns=daily_returns,
        trades=trades,
        initial_capital=initial_capital,
    )

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        daily_returns=daily_returns,
        metrics=metrics,
        friction_model=friction_model,
    )


def _compute_metrics(
    *,
    equity_curve: pd.Series,
    daily_returns: pd.Series,
    trades: list[Trade],
    initial_capital: float,
) -> BacktestMetrics:
    num_trades = len(trades)
    final_equity = equity_curve.iloc[-1] if len(equity_curve) else initial_capital

    total_return = _finite_or_none((final_equity - initial_capital) / initial_capital)

    num_days = (equity_curve.index[-1] - equity_curve.index[0]).days if len(equity_curve) > 1 else 0
    if num_days > 0 and final_equity > 0 and initial_capital > 0:
        cagr = _finite_or_none((final_equity / initial_capital) ** (365.25 / num_days) - 1)
    else:
        cagr = None

    if len(daily_returns) >= 2 and daily_returns.std(ddof=0) > 0:
        sharpe = _finite_or_none(
            daily_returns.mean() / daily_returns.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
        )
    else:
        sharpe = None

    if len(equity_curve):
        running_max = equity_curve.cummax()
        drawdown = (running_max - equity_curve) / running_max.replace(0, np.nan)
        max_drawdown = _finite_or_none(drawdown.max())
    else:
        max_drawdown = None

    if num_trades > 0:
        wins = [t for t in trades if t.net_pnl > 0]
        win_rate = _finite_or_none(len(wins) / num_trades)
    else:
        win_rate = None

    gross_wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_losses = sum(-t.net_pnl for t in trades if t.net_pnl < 0)
    profit_factor = _finite_or_none(gross_wins / gross_losses) if gross_losses > 0 else None

    return BacktestMetrics(
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_factor=profit_factor,
        num_trades=num_trades,
    )
