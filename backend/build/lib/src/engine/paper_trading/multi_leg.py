"""F&O Multi-Leg Execution (Build Spec §11): no atomic multi-leg primitive
exists in this engine, or anywhere in this codebase. Each leg of a
multi-leg order (e.g. a bull call spread) is filled *independently* by
walking its own order book; if some legs fill fully and others only
partially (or not at all), the result surfaces every leg's outcome
honestly.

Nothing here retries a partial/failed leg, and nothing here rolls back a
leg that already filled to "undo" an unbalanced spread -- this mirrors
real exchange behavior: a plain multi-leg order submission offers no
cross-leg atomicity either, unless a specific combo/strategy order type is
used (which this engine does not model). The caller
(`src.orchestration.paper_trading`) is responsible for surfacing an
unbalanced-spread outcome honestly -- e.g. as a flagged, inspectable
result -- never for silently pretending the spread completed as intended.
"""

from dataclasses import dataclass

from src.engine.paper_trading.order_book import FillResult, OrderBookProvider, Side, simulate_fill


@dataclass(frozen=True, slots=True)
class LegOrder:
    symbol: str
    side: Side
    quantity: int
    mid_price: float


@dataclass(frozen=True, slots=True)
class LegFillResult:
    leg: LegOrder
    fill: FillResult


@dataclass(frozen=True, slots=True)
class MultiLegFillResult:
    legs: list[LegFillResult]

    @property
    def fully_filled(self) -> bool:
        """True only when every single leg filled completely -- a spread
        where 3 of 4 legs filled fully is NOT fully_filled."""
        return all(leg.fill.fully_filled for leg in self.legs)

    @property
    def any_filled(self) -> bool:
        return any(leg.fill.filled_quantity > 0 for leg in self.legs)


def execute_multi_leg_order(
    legs: list[LegOrder], *, book_provider: OrderBookProvider
) -> MultiLegFillResult:
    """Executes each leg independently, in the order given, against its
    own order book snapshot. See module docstring: no retry, no rollback,
    no cross-leg coupling of any kind."""
    results: list[LegFillResult] = []
    for leg in legs:
        book = book_provider.snapshot(leg.symbol, mid_price=leg.mid_price)
        fill = simulate_fill(side=leg.side, quantity=leg.quantity, book=book)
        results.append(LegFillResult(leg=leg, fill=fill))
    return MultiLegFillResult(legs=results)
