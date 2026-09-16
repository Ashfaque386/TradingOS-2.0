"""Monte Carlo simulation tests (Build Spec §10): the 95th-percentile max
drawdown checked against known, hand-verifiable distributions -- a
uniform trade-P&L population makes every resampled path identical, so the
result is exactly computable rather than merely "plausible."
"""

import pytest

from src.engine.optimization.monte_carlo import run_monte_carlo


def test_empty_trade_list_never_fabricates_a_result():
    result = run_monte_carlo([], n_paths=500)

    assert result.percentile_95_max_drawdown is None
    assert result.mean_final_pnl is None
    assert result.median_final_pnl is None
    assert result.worst_path_max_drawdown is None


def test_single_guaranteed_loss_trade_gives_an_exact_deterministic_drawdown():
    # Every resampled path draws the same one value -> every path's
    # drawdown is identical and exactly computable by hand:
    # (1000 - 900) / 1000 = 0.1.
    result = run_monte_carlo([-100.0], n_paths=300, initial_capital=1000.0, seed=7)

    assert result.percentile_95_max_drawdown == pytest.approx(0.1)
    assert result.worst_path_max_drawdown == pytest.approx(0.1)
    assert result.mean_final_pnl == pytest.approx(-100.0)


def test_single_guaranteed_gain_trade_has_zero_drawdown():
    result = run_monte_carlo([100.0], n_paths=300, initial_capital=1000.0, seed=7)

    assert result.percentile_95_max_drawdown == pytest.approx(0.0)
    assert result.mean_final_pnl == pytest.approx(100.0)


def test_all_identical_trade_values_are_fully_deterministic_regardless_of_order():
    # 3 identical trades, each -50: equity path is always 1000->950->900->850
    # regardless of resampling order, so drawdown is exactly (1000-850)/1000.
    result = run_monte_carlo([-50.0, -50.0, -50.0], n_paths=500, initial_capital=1000.0, seed=3)

    assert result.percentile_95_max_drawdown == pytest.approx(0.15)


def test_more_paths_converges_toward_the_true_expected_value():
    pnls = [100.0, -50.0, 75.0, -25.0, 50.0]
    true_mean_per_trade = sum(pnls) / len(pnls)

    result = run_monte_carlo(pnls, n_paths=20_000, initial_capital=10_000.0, seed=42)

    # E[final pnl] = n_trades * mean(pnls) since each path draws n_trades
    # i.i.d. samples with replacement from the same population.
    expected_mean_final_pnl = len(pnls) * true_mean_per_trade
    assert result.mean_final_pnl == pytest.approx(expected_mean_final_pnl, abs=15.0)


def test_same_seed_is_reproducible():
    pnls = [10.0, -20.0, 30.0, -5.0]
    r1 = run_monte_carlo(pnls, n_paths=1000, seed=99)
    r2 = run_monte_carlo(pnls, n_paths=1000, seed=99)

    assert r1 == r2
