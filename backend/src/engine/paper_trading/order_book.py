"""Depth-Walked Fill Simulation (Build Spec §11): a paper order is filled
by walking a Level-2-style order book level by level, consuming whatever
depth actually exists at each price -- never assuming a full fill just
because the requested quantity says so. When the book's total depth on
the relevant side is less than the requested quantity, the fill is
honestly partial: `filled_quantity < requested_quantity`,
`remaining_quantity > 0`, surfaced on the result, never silently rounded
up to "filled."

`OrderBookProvider` is a `Protocol` (same swap-later posture as every
other not-yet-built external integration in this codebase);
`MockOrderBookProvider` is a deterministic synthetic stand-in -- the real
L2 feed is a Phase 8 broker-adapter / Phase 10 data-ingestion concern.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol

Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    quantity: int


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """`bids`/`asks` ordered best-to-worst (bids descending, asks
    ascending) -- the same convention any real L2 feed already provides."""

    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FillResult:
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    avg_fill_price: float | None  # None only when nothing filled at all
    fully_filled: bool


def simulate_fill(*, side: Side, quantity: int, book: OrderBookSnapshot) -> FillResult:
    """A BUY walks the asks (consuming liquidity offered for sale); a SELL
    walks the bids. Levels are consumed in the order given -- callers are
    responsible for supplying them best-to-worst, matching a real L2
    feed's own ordering."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    levels = book.asks if side == "buy" else book.bids

    remaining = quantity
    total_cost = 0.0
    filled = 0
    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity)
        total_cost += take * level.price
        filled += take
        remaining -= take

    avg_price = (total_cost / filled) if filled > 0 else None
    return FillResult(
        requested_quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=remaining,
        avg_fill_price=avg_price,
        fully_filled=(remaining == 0),
    )


class OrderBookProvider(Protocol):
    def snapshot(self, symbol: str, *, mid_price: float) -> OrderBookSnapshot: ...


@dataclass(frozen=True, slots=True)
class MockOrderBookProvider:
    """Deterministic synthetic depth centered on `mid_price` -- fixed
    quantity per level, evenly tick-spaced. Real depth is highly variable
    and asymmetric; this stand-in intentionally is not, and is documented
    as such rather than pretending otherwise."""

    levels: int = 5
    tick_size: float = 0.05
    quantity_per_level: int = 100

    def snapshot(self, symbol: str, *, mid_price: float) -> OrderBookSnapshot:
        bids = [
            OrderBookLevel(
                price=round(mid_price - self.tick_size * (i + 1), 2),
                quantity=self.quantity_per_level,
            )
            for i in range(self.levels)
        ]
        asks = [
            OrderBookLevel(
                price=round(mid_price + self.tick_size * (i + 1), 2),
                quantity=self.quantity_per_level,
            )
            for i in range(self.levels)
        ]
        return OrderBookSnapshot(bids=bids, asks=asks)
