"""Universal stop-loss tests (Build Spec §11)."""

from src.engine.paper_trading.stop_loss import check_universal_stop_loss


def test_long_position_stops_out_below_threshold():
    result = check_universal_stop_loss(
        position_qty=100, avg_cost=100.0, tick_price=96.5, stop_loss_pct=3.0
    )
    assert result.triggered is True


def test_long_position_does_not_stop_out_above_threshold():
    result = check_universal_stop_loss(
        position_qty=100, avg_cost=100.0, tick_price=98.0, stop_loss_pct=3.0
    )
    assert result.triggered is False


def test_short_position_stops_out_above_threshold():
    result = check_universal_stop_loss(
        position_qty=-100, avg_cost=100.0, tick_price=104.0, stop_loss_pct=3.0
    )
    assert result.triggered is True


def test_short_position_does_not_stop_out_below_threshold():
    result = check_universal_stop_loss(
        position_qty=-100, avg_cost=100.0, tick_price=101.0, stop_loss_pct=3.0
    )
    assert result.triggered is False


def test_flat_position_never_triggers():
    result = check_universal_stop_loss(
        position_qty=0, avg_cost=0.0, tick_price=50.0, stop_loss_pct=3.0
    )
    assert result.triggered is False


def test_exactly_at_threshold_triggers():
    result = check_universal_stop_loss(
        position_qty=100, avg_cost=100.0, tick_price=97.0, stop_loss_pct=3.0
    )
    assert result.triggered is True
