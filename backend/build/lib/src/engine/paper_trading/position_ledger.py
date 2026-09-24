"""Average-Cost-Basis Position Ledger (Build Spec §11): every fill updates
a single running (quantity, average_cost) pair per symbol -- a deliberate
simplification vs FIFO lot matching, where each individual buy lot would
be tracked and closed out in purchase order. Average-cost accounting is
what most retail brokerage/paper-trading statements actually show and is
far simpler to reason about and test than lot-level FIFO; the tradeoff is
that it can't answer "which specific lot was this sale against" the way
FIFO can, and its realized-P&L attribution on a partial close differs
from FIFO's once the average cost meaningfully diverges from the oldest
lot's cost (e.g. after several buys at different prices). This is a
conscious, documented choice, not an oversight -- FIFO lot matching is
real added complexity this phase's scope doesn't need.

Quantity is signed: positive is long, negative is short (F&O selling-to-
open is a legitimate paper position, not just equities going long).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Position:
    quantity: int  # signed: +long, -short
    avg_cost: float  # meaningless (kept at 0) when quantity == 0


@dataclass(frozen=True, slots=True)
class FillApplication:
    new_position: Position
    realized_pnl: float


def apply_fill(
    position: Position, *, side: str, fill_quantity: int, fill_price: float
) -> FillApplication:
    if fill_quantity <= 0:
        raise ValueError("fill_quantity must be positive")

    signed_fill = fill_quantity if side == "buy" else -fill_quantity
    old_qty = position.quantity
    old_cost = position.avg_cost
    new_qty = old_qty + signed_fill

    if old_qty == 0:
        # Flat before this fill: opens a fresh position in either direction.
        return FillApplication(
            new_position=Position(quantity=new_qty, avg_cost=fill_price), realized_pnl=0.0
        )

    same_direction = (old_qty > 0 and signed_fill > 0) or (old_qty < 0 and signed_fill < 0)

    if same_direction:
        # Adding to the position: weighted-average cost across old + new.
        total_cost = abs(old_qty) * old_cost + fill_quantity * fill_price
        new_avg_cost = total_cost / (abs(old_qty) + fill_quantity)
        return FillApplication(
            new_position=Position(quantity=new_qty, avg_cost=new_avg_cost), realized_pnl=0.0
        )

    # Opposite direction: this fill closes some/all of the existing
    # position, and may flip into the opposite direction if it overshoots.
    closing_qty = min(fill_quantity, abs(old_qty))
    if old_qty > 0:
        realized = (fill_price - old_cost) * closing_qty
    else:
        realized = (old_cost - fill_price) * closing_qty

    if fill_quantity <= abs(old_qty):
        # Partial or exact close -- the remaining position keeps the same
        # average cost (nothing new was added to it).
        remaining_avg_cost = old_cost if new_qty != 0 else 0.0
        return FillApplication(
            new_position=Position(quantity=new_qty, avg_cost=remaining_avg_cost),
            realized_pnl=realized,
        )

    # Flip: the excess quantity opens a brand-new position in the opposite
    # direction at this fill's price.
    return FillApplication(
        new_position=Position(quantity=new_qty, avg_cost=fill_price), realized_pnl=realized
    )
