"""Monte Carlo trade-resampling simulation (Build Spec §10): resamples a
strategy's own trade P&L distribution (with replacement) into `n_paths`
synthetic equity paths, and takes the **95th percentile of each path's
own max drawdown** -- not the strategy's single historical max drawdown
-- as the number that should govern position sizing. A backtest's one
realized sequence of trades is one draw from a much wider distribution of
equally-likely trade orderings; resampling estimates how bad a
95th-percentile-unlucky ordering could plausibly have been, which is
usually materially worse (and more honest for sizing) than "the one
drawdown that happened to occur historically."

Distributed via Temporal for production-scale runs -- 10,000 paths over a
handful of trades is fast in-process, but real compute at scale (many
strategies, many trades, or more paths). See `temporal_workflows.py` for
the distribution wrapper. The statistical core here is a pure,
synchronous, numpy-vectorized function with **no Temporal dependency**,
so it is fully testable (and directly callable in-process) without a
Temporal server.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    n_paths: int
    percentile_95_max_drawdown: float | None
    mean_final_pnl: float | None
    median_final_pnl: float | None
    worst_path_max_drawdown: float | None


def _finite_or_none(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def run_monte_carlo(
    trade_pnls: list[float],
    *,
    n_paths: int = 10_000,
    initial_capital: float = 100_000.0,
    seed: int | None = None,
) -> MonteCarloResult:
    n_trades = len(trade_pnls)
    if n_trades == 0:
        return MonteCarloResult(
            n_paths=n_paths,
            percentile_95_max_drawdown=None,
            mean_final_pnl=None,
            median_final_pnl=None,
            worst_path_max_drawdown=None,
        )

    rng = np.random.default_rng(seed)
    pnls = np.asarray(trade_pnls, dtype=float)

    # Resample with replacement: n_trades draws per path, n_paths paths at
    # once -- one vectorized draw, not a Python loop over paths.
    sampled = rng.choice(pnls, size=(n_paths, n_trades), replace=True)
    equity = initial_capital + np.cumsum(sampled, axis=1)
    # Prepend the starting capital so bar 0 is part of the drawdown calc.
    equity = np.concatenate([np.full((n_paths, 1), initial_capital), equity], axis=1)

    running_max = np.maximum.accumulate(equity, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = (running_max - equity) / running_max
    max_drawdown_per_path = np.nanmax(drawdowns, axis=1)
    final_pnl_per_path = equity[:, -1] - initial_capital

    return MonteCarloResult(
        n_paths=n_paths,
        percentile_95_max_drawdown=_finite_or_none(np.percentile(max_drawdown_per_path, 95)),
        mean_final_pnl=_finite_or_none(np.mean(final_pnl_per_path)),
        median_final_pnl=_finite_or_none(np.median(final_pnl_per_path)),
        worst_path_max_drawdown=_finite_or_none(np.max(max_drawdown_per_path)),
    )
