"""Depth-walked fill simulation tests (Build Spec §11): the acceptance-
critical case is honest partial-filling under thin depth -- never a
silently-assumed full fill.
"""

import pytest

from src.engine.paper_trading.order_book import (
    MockOrderBookProvider,
    OrderBookLevel,
    OrderBookSnapshot,
    simulate_fill,
)


def _book():
    return OrderBookSnapshot(
        bids=[OrderBookLevel(99.9, 50), OrderBookLevel(99.8, 50)],
        asks=[OrderBookLevel(100.1, 50), OrderBookLevel(100.2, 50)],
    )


def test_full_fill_within_available_depth():
    result = simulate_fill(side="buy", quantity=50, book=_book())
    assert result.filled_quantity == 50
    assert result.remaining_quantity == 0
    assert result.fully_filled is True
    assert result.avg_fill_price == 100.1


def test_thin_depth_produces_an_honest_partial_fill():
    result = simulate_fill(side="buy", quantity=150, book=_book())
    assert result.requested_quantity == 150
    assert result.filled_quantity == 100  # only 100 total available on asks
    assert result.remaining_quantity == 50
    assert result.fully_filled is False
    assert result.avg_fill_price == pytest.approx((50 * 100.1 + 50 * 100.2) / 100)


def test_sell_walks_the_bid_side():
    result = simulate_fill(side="sell", quantity=75, book=_book())
    assert result.filled_quantity == 75
    assert result.fully_filled is True
    assert result.avg_fill_price == pytest.approx((50 * 99.9 + 25 * 99.8) / 75)


def test_empty_book_fills_nothing_never_fabricates_a_price():
    result = simulate_fill(side="buy", quantity=10, book=OrderBookSnapshot())
    assert result.filled_quantity == 0
    assert result.remaining_quantity == 10
    assert result.fully_filled is False
    assert result.avg_fill_price is None


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        simulate_fill(side="buy", quantity=0, book=_book())


def test_mock_order_book_provider_centers_on_mid_price():
    provider = MockOrderBookProvider(levels=3, tick_size=0.05, quantity_per_level=100)
    snap = provider.snapshot("DEMO", mid_price=2000.0)
    assert len(snap.bids) == 3
    assert len(snap.asks) == 3
    assert snap.bids[0].price < 2000.0 < snap.asks[0].price
