"""Volatility-Adjusted Position Sizing tests (Build Spec §8): hand-verified
against the documented formula.
"""

from src.engine.risk.position_sizing import size_position


def test_hand_calculated_sizing():
    result = size_position(
        account_equity=1_000_000, price=2_500, atr=20, risk_per_trade_pct=1.0, atr_multiplier=2.0
    )
    assert result.risk_amount == 10_000
    assert result.stop_distance == 40
    assert result.quantity == 250


def test_higher_volatility_produces_a_smaller_position_for_the_same_risk_budget():
    calm = size_position(account_equity=1_000_000, price=2_500, atr=20, risk_per_trade_pct=1.0)
    volatile = size_position(account_equity=1_000_000, price=2_500, atr=200, risk_per_trade_pct=1.0)
    assert volatile.quantity < calm.quantity
    assert volatile.quantity == calm.quantity // 10


def test_never_fabricates_a_size_with_invalid_inputs():
    assert size_position(account_equity=0, price=2_500, atr=20).quantity == 0
    assert size_position(account_equity=1_000_000, price=0, atr=20).quantity == 0
    assert size_position(account_equity=1_000_000, price=2_500, atr=0).quantity == 0
