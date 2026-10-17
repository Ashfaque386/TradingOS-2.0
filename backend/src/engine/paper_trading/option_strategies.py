"""The option structures paper trading builds from a daily signal (real-market-data plan, phase 5).

Defined risk only: no structure can lose more than a known amount. Nothing here sells an option
that is not hedged by one it also buys.

- `long_option`: a BUY signal buys the at-the-money call, a SELL signal buys the at-the-money put.
  The most it can lose is the premium.
- `vertical_spread`: a *credit* spread. It sells the at-the-money option and buys one of the same
  type and expiry `width` strikes further out of the money as its hedge -- a bull put spread on
  BUY (sell the put, buy a lower one), a bear call spread on SELL (sell the call, buy a higher one).
  The most it can lose is the spread's width less the credit taken in. This is the shape the
  naked-options scan accepts: a sold leg must have a bought leg further out of the money.

The contract is the soonest expiry that still has at least `min_days` days to run (a near-dated
option decays too fast to be worth buying); the at-the-money strike is the listed strike nearest
the underlying's price (the lower one on a tie). Expiries and strikes are whatever the broker's
instrument master lists -- never assumed to fall on any weekday or step.

These are pure functions of the contract list, the spot price and the date: what gets executed,
and how, is `src.orchestration.paper_options`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from src.brokers.base import OptionContract, parse_option_symbol
from src.engine.paper_trading.order_book import Side

OPTION_STRATEGIES = ("long_option", "vertical_spread")
Direction = Literal["bullish", "bearish"]


@dataclass(frozen=True, slots=True)
class LegPlan:
    contract: OptionContract
    side: Side
    quantity: int


@dataclass(frozen=True, slots=True)
class EntryPlan:
    strategy: str
    direction: Direction
    expiry: date
    legs: tuple[LegPlan, ...]  # bought legs first: a sold leg is never opened before its hedge


def direction_for_signal(signal_type: str) -> Direction:
    """BUY is a bullish view (calls), SELL a bearish one (puts)."""
    return "bullish" if signal_type == "BUY" else "bearish"


def pick_expiry(contracts: Sequence[OptionContract], *, today: date, min_days: int) -> date | None:
    """The soonest expiry with at least `min_days` days left."""
    expiries = sorted({c.expiry for c in contracts if (c.expiry - today).days >= min_days})
    return expiries[0] if expiries else None


def nearest_strike(strikes: Sequence[float], spot: float) -> float | None:
    """The listed strike closest to `spot` (the lower one when two are equally close)."""
    if not strikes:
        return None
    return min(sorted(strikes), key=lambda strike: abs(strike - spot))


def plan_entry(
    strategy: str,
    signal_type: str,
    *,
    spot: float,
    contracts: Sequence[OptionContract],
    today: date,
    min_days: int,
    lots: int,
    width: int,
) -> EntryPlan | None:
    """The legs to open for `signal_type`, or `None` when no suitable contract is listed (no
    expiry far enough out, no strikes, a spread whose sold strike does not exist)."""
    if strategy not in OPTION_STRATEGIES:
        raise ValueError(f"unknown option strategy {strategy!r}")
    expiry = pick_expiry(contracts, today=today, min_days=min_days)
    if expiry is None or lots <= 0:
        return None
    direction = direction_for_signal(signal_type)
    if strategy == "long_option":
        kind = "CE" if direction == "bullish" else "PE"
    else:
        # A credit spread sells the option that gains from the view: a put when bullish, a call
        # when bearish.
        kind = "PE" if direction == "bullish" else "CE"
    chain = {c.strike: c for c in contracts if c.expiry == expiry and c.option_type == kind}
    atm = nearest_strike(list(chain), spot)
    if atm is None:
        return None
    at_the_money = chain[atm]
    if strategy == "long_option":
        legs = [LegPlan(at_the_money, "buy", lots * at_the_money.lot_size)]
        return EntryPlan(strategy, direction, expiry, tuple(legs))
    strikes = sorted(chain)
    position = strikes.index(atm)
    # The hedge is further out of the money: a higher strike for calls, a lower one for puts.
    target = position + width if kind == "CE" else position - width
    if width <= 0 or not 0 <= target < len(strikes):
        return None
    hedge = chain[strikes[target]]
    if hedge.lot_size != at_the_money.lot_size:
        return None  # the two legs would not cover each other
    return EntryPlan(
        strategy,
        direction,
        expiry,
        # The bought hedge first: the sold leg is never opened before it.
        (
            LegPlan(hedge, "buy", lots * hedge.lot_size),
            LegPlan(at_the_money, "sell", lots * at_the_money.lot_size),
        ),
    )


def held_direction(open_legs: Sequence[tuple[str, int]]) -> Direction | None:
    """The view a set of held legs expresses, from `(contract symbol, signed quantity)` pairs.
    A sold leg decides it when there is one (a credit spread's view is its sold option: a sold
    put is bullish, a sold call bearish; the bought leg is only its hedge); otherwise the bought
    leg does (a bought call is bullish, a bought put bearish)."""
    parsed = [
        (info[3], quantity)
        for symbol, quantity in open_legs
        if quantity != 0 and (info := parse_option_symbol(symbol)) is not None
    ]
    for kind, quantity in parsed:
        if quantity < 0:
            return "bearish" if kind == "CE" else "bullish"
    for kind, quantity in parsed:
        if quantity > 0:
            return "bullish" if kind == "CE" else "bearish"
    return None


def structure_value(legs: Sequence[tuple[int, float]]) -> float:
    """Sum of signed quantity times price over `(signed quantity, price)` legs: what the legs cost
    to open when priced at their average cost (a net debit), or are worth when priced at the
    market."""
    return sum(quantity * price for quantity, price in legs)


def max_loss(plan_legs: Sequence[LegPlan], *, net_debit: float) -> float:
    """The most a planned structure can lose, given the net debit it was opened at (negative for a
    credit). A structure with only bought legs can lose what it cost. A vertical credit spread can
    lose its width in rupees (strike gap x quantity) less the credit taken in."""
    sold = [leg for leg in plan_legs if leg.side == "sell"]
    if not sold:
        return net_debit
    bought = [leg for leg in plan_legs if leg.side == "buy"]
    width_value = max(
        abs(s.contract.strike - b.contract.strike) * min(s.quantity, b.quantity)
        for s in sold
        for b in bought
    )
    return width_value + net_debit


def stop_triggered(*, risk: float, loss: float, stop_pct: float) -> bool:
    """True when the structure has lost `stop_pct` percent or more of what it can lose at most
    (`risk`: the premium for a bought option, the width less the credit for a credit spread).
    A structure with nothing at risk never triggers."""
    if risk <= 0:
        return False
    return loss >= risk * stop_pct / 100.0
