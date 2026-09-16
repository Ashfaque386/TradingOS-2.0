from datetime import date

import pandas as pd

from src.engine.backtest.corporate_actions import CorporateAction, back_adjust_for_corporate_actions


def _prices() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "open": [200, 200, 200, 100, 100],
            "high": [200, 200, 200, 100, 100],
            "low": [200, 200, 200, 100, 100],
            "close": [200, 200, 200, 100, 100],
            "volume": [1000, 1000, 1000, 2000, 2000],
        },
        index=idx,
    )


def test_split_back_adjusts_pre_split_bars_to_match_post_split_level():
    actions = [CorporateAction(ex_date=date(2024, 1, 4), ratio=0.5)]
    adjusted = back_adjust_for_corporate_actions(_prices(), actions)

    assert (adjusted.loc["2024-01-01":"2024-01-03", "close"] == 100).all()
    assert (adjusted.loc["2024-01-04":"2024-01-05", "close"] == 100).all()
    assert (adjusted.loc["2024-01-01":"2024-01-03", "volume"] == 2000).all()
    assert (adjusted.loc["2024-01-04":"2024-01-05", "volume"] == 2000).all()


def test_bars_on_or_after_ex_date_are_untouched():
    actions = [CorporateAction(ex_date=date(2024, 1, 4), ratio=0.5)]
    adjusted = back_adjust_for_corporate_actions(_prices(), actions)
    original = _prices()

    assert (adjusted.loc["2024-01-04":, "close"] == original.loc["2024-01-04":, "close"]).all()


def test_multiple_actions_compound_for_the_earliest_bars():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    prices = pd.DataFrame(
        {
            "open": [400, 200, 100],
            "high": [400, 200, 100],
            "low": [400, 200, 100],
            "close": [400, 200, 100],
            "volume": [500, 1000, 2000],
        },
        index=idx,
    )
    actions = [
        CorporateAction(ex_date=date(2024, 1, 2), ratio=0.5),
        CorporateAction(ex_date=date(2024, 1, 3), ratio=0.5),
    ]
    adjusted = back_adjust_for_corporate_actions(prices, actions)

    # Day 1 is before both actions -> both ratios compound: 400*0.5*0.5=100.
    assert adjusted.loc["2024-01-01", "close"] == 100
    # Day 2 is before only the second action -> 200*0.5=100.
    assert adjusted.loc["2024-01-02", "close"] == 100
    # Day 3 is after both -> untouched.
    assert adjusted.loc["2024-01-03", "close"] == 100


def test_original_dataframe_is_not_mutated():
    original = _prices()
    original_copy = original.copy()
    back_adjust_for_corporate_actions(
        original, [CorporateAction(ex_date=date(2024, 1, 4), ratio=0.5)]
    )
    pd.testing.assert_frame_equal(original, original_copy)


def test_corporate_action_rejects_non_positive_ratio():
    import pytest

    with pytest.raises(ValueError):
        CorporateAction(ex_date=date(2024, 1, 1), ratio=0.0)
    with pytest.raises(ValueError):
        CorporateAction(ex_date=date(2024, 1, 1), ratio=-1.0)
