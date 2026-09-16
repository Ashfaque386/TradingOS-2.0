"""Correlation Constraint (Build Spec §8): rejects a proposed strategy or
live/paper position whose returns are correlated to the Nifty 50 beyond a
configurable threshold (default 0.85). This module is deliberately the
*only* place that correlation-to-benchmark logic lives -- both call sites
(`src.orchestration.strategies.promote_to_paper_trading`'s pre-deployment
check and `src.orchestration.risk_gate`'s live/paper per-tick check) import
and call the same `evaluate_correlation_constraint` function below. An
earlier build wired this into only the pre-deployment check and missed the
per-tick gate; this module's whole shape exists to make that gap
structurally impossible to repeat -- there's only one function to call,
not two implementations that could drift apart.

Correlation, not anti-correlation: a strategy that moves strongly *against*
the index isn't the concentration risk this constraint targets (a
long-biased book crowded into the same trade as "the market"), so the
breach condition is `correlation > threshold`, not `abs(correlation) >
threshold`.

Never fabricates a verdict: fewer than `MIN_OVERLAPPING_DAYS` overlapping
observations (or an undefined correlation, e.g. from a zero-variance
series) yields `correlation=None`, `evaluated=False`, `breached=False` --
distinguishable from a genuine below-threshold pass, never silently
mistaken for one.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

DEFAULT_CORRELATION_THRESHOLD = 0.85
MIN_OVERLAPPING_DAYS = 10


@dataclass(frozen=True, slots=True)
class CorrelationCheckResult:
    evaluated: bool
    correlation: float | None
    threshold: float
    breached: bool


def evaluate_correlation_constraint(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> CorrelationCheckResult:
    aligned = pd.concat([returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(aligned) < MIN_OVERLAPPING_DAYS:
        return CorrelationCheckResult(
            evaluated=False, correlation=None, threshold=threshold, breached=False
        )

    corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    if pd.isna(corr):
        return CorrelationCheckResult(
            evaluated=False, correlation=None, threshold=threshold, breached=False
        )

    corr = float(corr)
    return CorrelationCheckResult(
        evaluated=True, correlation=corr, threshold=threshold, breached=corr > threshold
    )


class NiftyBenchmarkProvider(Protocol):
    def daily_returns(self) -> pd.Series: ...


@dataclass(frozen=True, slots=True)
class FakeNiftyBenchmarkProvider:
    """Deterministic, seeded synthetic Nifty 50 daily-returns stand-in --
    the real index feed is a Phase 10 data-ingestion concern. Swapping in a
    real provider later needs no change at either call site: both just
    call `.daily_returns()` on whatever `NiftyBenchmarkProvider` they're
    given."""

    returns: pd.Series

    def daily_returns(self) -> pd.Series:
        return self.returns


def make_fake_nifty_benchmark(
    index: pd.DatetimeIndex,
    *,
    seed: int = 0,
    daily_mean: float = 0.0004,
    daily_vol: float = 0.01,
) -> FakeNiftyBenchmarkProvider:
    rng = np.random.default_rng(seed)
    returns = pd.Series(rng.normal(daily_mean, daily_vol, len(index)), index=index)
    return FakeNiftyBenchmarkProvider(returns=returns)
