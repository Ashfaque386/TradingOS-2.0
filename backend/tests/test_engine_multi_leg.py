"""F&O multi-leg execution tests (Build Spec §11): the acceptance-critical
case is that a partial fill on one leg is surfaced honestly and never
triggers a retry or a rollback of the other leg(s).
"""

from dataclasses import dataclass, field

from src.engine.paper_trading.multi_leg import LegOrder, execute_multi_leg_order
from src.engine.paper_trading.order_book import OrderBookLevel, OrderBookSnapshot


@dataclass(frozen=True)
class _ThinBookProvider:
    thin_symbols: frozenset
    thin_quantity: int = 5
    full_quantity: int = 100
    calls: list = field(default_factory=list, compare=False)

    def snapshot(self, symbol, *, mid_price):
        self.calls.append(symbol)
        qty = self.thin_quantity if symbol in self.thin_symbols else self.full_quantity
        return OrderBookSnapshot(
            bids=[OrderBookLevel(mid_price - 0.05, qty)],
            asks=[OrderBookLevel(mid_price + 0.05, qty)],
        )


def test_all_legs_fill_when_depth_is_sufficient():
    provider = _ThinBookProvider(thin_symbols=frozenset())
    legs = [
        LegOrder(symbol="LEG_A", side="sell", quantity=50, mid_price=100.0),
        LegOrder(symbol="LEG_B", side="buy", quantity=50, mid_price=105.0),
    ]
    result = execute_multi_leg_order(legs, book_provider=provider)
    assert result.fully_filled is True
    assert all(lf.fill.fully_filled for lf in result.legs)


def test_one_partial_leg_makes_the_whole_spread_not_fully_filled():
    provider = _ThinBookProvider(thin_symbols=frozenset({"LEG_B"}))
    legs = [
        LegOrder(symbol="LEG_A", side="sell", quantity=50, mid_price=100.0),
        LegOrder(symbol="LEG_B", side="buy", quantity=50, mid_price=105.0),
    ]
    result = execute_multi_leg_order(legs, book_provider=provider)

    assert result.legs[0].fill.fully_filled is True
    assert result.legs[0].fill.filled_quantity == 50

    assert result.legs[1].fill.fully_filled is False
    assert result.legs[1].fill.filled_quantity == 5
    assert result.legs[1].fill.remaining_quantity == 45

    assert result.fully_filled is False
    assert result.any_filled is True


def test_a_partial_leg_is_never_retried():
    """Each leg's book is snapshotted exactly once -- proof there is no
    retry loop anywhere in execute_multi_leg_order."""
    provider = _ThinBookProvider(thin_symbols=frozenset({"LEG_B"}))
    legs = [
        LegOrder(symbol="LEG_A", side="sell", quantity=50, mid_price=100.0),
        LegOrder(symbol="LEG_B", side="buy", quantity=50, mid_price=105.0),
    ]
    execute_multi_leg_order(legs, book_provider=provider)
    assert provider.calls == ["LEG_A", "LEG_B"]


def test_a_fully_unfilled_leg_does_not_block_other_legs():
    provider = _ThinBookProvider(thin_symbols=frozenset({"LEG_A"}), thin_quantity=0)
    legs = [
        LegOrder(symbol="LEG_A", side="sell", quantity=50, mid_price=100.0),
        LegOrder(symbol="LEG_B", side="buy", quantity=50, mid_price=105.0),
    ]
    result = execute_multi_leg_order(legs, book_provider=provider)
    assert result.legs[0].fill.filled_quantity == 0
    assert (
        result.legs[1].fill.fully_filled is True
    ), "leg B must still execute despite leg A filling nothing"
