"""Average-cost-basis position ledger tests (Build Spec §11): hand-worked
examples, including a same-tick position flip.
"""

import pytest

from src.engine.paper_trading.position_ledger import Position, apply_fill


def test_opening_a_fresh_long_position():
    result = apply_fill(Position(0, 0.0), side="buy", fill_quantity=100, fill_price=10.0)
    assert result.new_position == Position(100, 10.0)
    assert result.realized_pnl == 0.0


def test_adding_to_a_long_position_averages_the_cost():
    pos = Position(100, 10.0)
    result = apply_fill(pos, side="buy", fill_quantity=100, fill_price=20.0)
    assert result.new_position.quantity == 200
    assert result.new_position.avg_cost == pytest.approx(15.0)
    assert result.realized_pnl == 0.0


def test_partial_close_realizes_pnl_and_keeps_the_same_avg_cost():
    pos = Position(200, 15.0)
    result = apply_fill(pos, side="sell", fill_quantity=50, fill_price=25.0)
    assert result.new_position.quantity == 150
    assert result.new_position.avg_cost == pytest.approx(15.0)
    assert result.realized_pnl == pytest.approx((25.0 - 15.0) * 50)


def test_overselling_flips_to_a_short_position_at_the_fill_price():
    pos = Position(150, 15.0)
    result = apply_fill(pos, side="sell", fill_quantity=200, fill_price=30.0)
    assert result.new_position.quantity == -50
    assert result.new_position.avg_cost == pytest.approx(30.0)
    assert result.realized_pnl == pytest.approx((30.0 - 15.0) * 150)


def test_covering_a_short_position_realizes_pnl():
    pos = Position(-50, 30.0)
    result = apply_fill(pos, side="buy", fill_quantity=50, fill_price=28.0)
    assert result.new_position.quantity == 0
    assert result.realized_pnl == pytest.approx((30.0 - 28.0) * 50)


def test_exact_close_zeroes_avg_cost():
    pos = Position(100, 10.0)
    result = apply_fill(pos, side="sell", fill_quantity=100, fill_price=12.0)
    assert result.new_position.quantity == 0
    assert result.new_position.avg_cost == 0.0
    assert result.realized_pnl == pytest.approx(200.0)


def test_opening_a_fresh_short_position():
    result = apply_fill(Position(0, 0.0), side="sell", fill_quantity=100, fill_price=50.0)
    assert result.new_position == Position(-100, 50.0)
    assert result.realized_pnl == 0.0


def test_fill_quantity_must_be_positive():
    with pytest.raises(ValueError):
        apply_fill(Position(0, 0.0), side="buy", fill_quantity=0, fill_price=10.0)
