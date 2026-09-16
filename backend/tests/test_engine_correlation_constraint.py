"""Correlation Constraint tests (Build Spec §8): fires identically wherever
it's called -- both the pre-deployment and per-tick contexts import and
call the exact same function under test here.
"""

import numpy as np
import pandas as pd

from src.engine.risk.correlation_constraint import (
    MIN_OVERLAPPING_DAYS,
    evaluate_correlation_constraint,
    make_fake_nifty_benchmark,
)


def test_highly_correlated_returns_breach_the_default_threshold():
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=1)
    correlated = bench.returns * 0.95 + 0.0001

    result = evaluate_correlation_constraint(correlated, bench.daily_returns())

    assert result.evaluated is True
    assert result.breached is True
    assert result.correlation > 0.85


def test_uncorrelated_returns_do_not_breach():
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=1)
    uncorrelated = pd.Series(np.random.default_rng(99).normal(0, 0.01, 30), index=idx)

    result = evaluate_correlation_constraint(uncorrelated, bench.daily_returns())

    assert result.evaluated is True
    assert result.breached is False


def test_never_fabricates_a_verdict_below_the_overlap_threshold():
    idx = pd.bdate_range("2024-01-01", periods=MIN_OVERLAPPING_DAYS - 1)
    bench = make_fake_nifty_benchmark(idx, seed=1)

    result = evaluate_correlation_constraint(bench.returns, bench.daily_returns())

    assert result.evaluated is False
    assert result.correlation is None
    assert result.breached is False


def test_threshold_is_configurable():
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=1)
    moderately_correlated = bench.returns * 0.5 + pd.Series(
        np.random.default_rng(5).normal(0, 0.005, 30), index=idx
    )

    lenient = evaluate_correlation_constraint(
        moderately_correlated, bench.daily_returns(), threshold=0.99
    )
    assert lenient.breached is False

    strict = evaluate_correlation_constraint(
        moderately_correlated, bench.daily_returns(), threshold=0.01
    )
    assert strict.breached is True


def test_zero_variance_series_never_fabricates_a_correlation():
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=1)
    constant = pd.Series([0.0] * 30, index=idx)

    result = evaluate_correlation_constraint(constant, bench.daily_returns())

    assert result.evaluated is False
    assert result.correlation is None
