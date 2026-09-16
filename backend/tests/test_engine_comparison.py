"""Multi-run comparison tests (Build Spec §10): null (never a fabricated
0 or 1) when fewer than 10 overlapping days.
"""

import numpy as np
import pandas as pd
import pytest

from src.engine.backtest.comparison import MIN_OVERLAPPING_DAYS, compare_runs


def test_null_when_fewer_than_ten_overlapping_days():
    idx = pd.bdate_range("2024-01-01", periods=9)
    r1 = pd.Series(np.linspace(0.01, 0.02, 9), index=idx)
    r2 = pd.Series(np.linspace(0.02, 0.01, 9), index=idx)

    matrix = compare_runs({"a": r1, "b": r2})

    assert matrix.get("a", "b") is None


def test_real_correlation_when_at_or_above_the_threshold():
    idx = pd.bdate_range("2024-01-01", periods=MIN_OVERLAPPING_DAYS)
    rng = np.random.default_rng(0)
    r1 = pd.Series(rng.normal(0, 0.01, MIN_OVERLAPPING_DAYS), index=idx)
    r2 = r1 * 0.9  # perfectly correlated by construction

    matrix = compare_runs({"a": r1, "b": r2})

    assert matrix.get("a", "b") == pytest.approx(1.0)


def test_null_when_overlap_is_nonempty_but_still_under_threshold_due_to_partial_dates():
    idx_a = pd.bdate_range("2024-01-01", periods=20)  # ends ~2024-01-26
    idx_b = pd.bdate_range("2024-01-24", periods=20)  # starts 3 bdays before idx_a ends
    r1 = pd.Series(np.linspace(0, 1, 20), index=idx_a)
    r2 = pd.Series(np.linspace(1, 0, 20), index=idx_b)

    matrix = compare_runs({"a": r1, "b": r2})

    assert matrix.get("a", "b") is None


def test_self_comparison_is_always_one():
    idx = pd.bdate_range("2024-01-01", periods=5)
    r1 = pd.Series([0.01, 0.02, -0.01, 0.03, 0.0], index=idx)

    matrix = compare_runs({"a": r1})

    assert matrix.get("a", "a") == 1.0


def test_constant_returns_yield_null_not_a_fabricated_correlation():
    idx = pd.bdate_range("2024-01-01", periods=MIN_OVERLAPPING_DAYS)
    r1 = pd.Series([0.0] * MIN_OVERLAPPING_DAYS, index=idx)  # zero variance
    r2 = pd.Series(np.random.default_rng(1).normal(0, 0.01, MIN_OVERLAPPING_DAYS), index=idx)

    matrix = compare_runs({"a": r1, "b": r2})

    assert matrix.get("a", "b") is None


def test_matrix_is_symmetric_lookup():
    idx = pd.bdate_range("2024-01-01", periods=MIN_OVERLAPPING_DAYS)
    r1 = pd.Series(np.linspace(0, 1, MIN_OVERLAPPING_DAYS), index=idx)
    r2 = pd.Series(np.linspace(1, 0, MIN_OVERLAPPING_DAYS), index=idx)

    matrix = compare_runs({"a": r1, "b": r2})

    assert matrix.get("a", "b") == matrix.get("b", "a")
