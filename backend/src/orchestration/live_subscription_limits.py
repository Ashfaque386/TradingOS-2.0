"""Tightening a live subscription's standing limits, or switching it off, after it was enrolled.

A live subscription carries limits that stand between a strategy and the broker (how many orders in
a window, how large one may be, how long an order lives). Making them **stricter** is something a
person should be able to do in a hurry, with a reason, and have audited. Making them **looser** is
not: that changes what the strategy is trusted with, so it is a fresh enrolment, not an edit. Each
rule below says which way a limit may move.

Two refusals protect an open position, because a strategy's exit and its stop-loss are orders too
and go through the same limits:

* a notional cap below what the strategy already holds would block the very order that closes it;
* a switched-off subscription generates no exit and no stop-loss order, so switching one off while
  it holds shares would leave them unwatched (and nothing here can close them: there is no manual
  live order). Autonomous trading can be turned off instead, which is reversible.

Nothing here switches a subscription back on, and nothing raises a limit. This module is the rules
alone, on a subscription already in hand; reading it, saving it and the audit entry are
`src.orchestration.live_subscription_updates`.
"""

from dataclasses import dataclass
from typing import Literal

from src.models.live_trading_subscription import LiveTradingSubscription

# How each limit may move: a smaller number is stricter, except for the window, where a longer one
# counts more orders against the same cap.
Direction = Literal["lower", "raise"]
LIMITS: dict[str, tuple[str, Direction]] = {
    "max_intents_per_window": ("orders per window", "lower"),
    "rate_limit_window_minutes": ("window in minutes", "raise"),
    "max_notional_per_intent": ("largest order in rupees", "lower"),
    "intent_expiry_seconds": ("seconds an order lives", "lower"),
}


class LimitChangeRefused(Exception):
    """The request would loosen a limit, change nothing, or leave a position unwatched (a 409)."""


@dataclass(frozen=True, slots=True)
class LimitChange:
    field: str
    before: float | int
    after: float | int


def plan_limit_changes(
    subscription: LiveTradingSubscription,
    requested: dict[str, float | int],
    *,
    held_quantity: int,
    held_cost: float,
) -> list[LimitChange]:
    """What would change, or why it may not. A value equal to the current one is not a change."""
    if not subscription.is_active:
        raise LimitChangeRefused(
            f"the {subscription.symbol} subscription is switched off: nothing is left to tighten. "
            "Enrol the strategy again to trade it."
        )
    changes: list[LimitChange] = []
    for name, value in requested.items():
        label, direction = LIMITS[name]
        before = getattr(subscription, name)
        if value == before:
            continue
        looser = value > before if direction == "lower" else value < before
        if looser:
            way = "lowered" if direction == "lower" else "raised"
            raise LimitChangeRefused(
                f"{label} is {before:g} and can only be {way} here, not set to {value:g}. "
                "Making a limit looser changes what the strategy is trusted with: enrol it again."
            )
        if name == "max_notional_per_intent" and held_quantity != 0 and value < held_cost:
            raise LimitChangeRefused(
                f"this strategy holds {abs(held_quantity):g} shares of {subscription.symbol} "
                f"(about ₹{held_cost:,.0f} at cost), and an order cap of ₹{value:,.0f} would block "
                "the order that closes them. Lower it after the position is closed."
            )
        changes.append(LimitChange(name, before, value))
    if not changes:
        raise LimitChangeRefused("nothing to change: every value is already what it is")
    return changes


def plan_deactivation(subscription: LiveTradingSubscription, *, held_quantity: int) -> None:
    """Raises `LimitChangeRefused` when the subscription cannot be switched off right now."""
    if not subscription.is_active:
        raise LimitChangeRefused(f"the {subscription.symbol} subscription is already switched off")
    if held_quantity != 0:
        raise LimitChangeRefused(
            f"this strategy holds {abs(held_quantity):g} shares of {subscription.symbol}, and a "
            "switched-off subscription generates no exit and no stop-loss order, so they would be "
            "left unwatched. Turn autonomous trading off instead (it can be turned back on), and "
            "switch the subscription off once the position is closed."
        )


def switch_off(subscription: LiveTradingSubscription) -> bool:
    """Switches the subscription off and ends its autonomy, and says whether autonomy was on. A
    switched-off subscription is not trading autonomously, whatever the switch said: the console's
    "is real money being risked right now" answer must not count it."""
    was_autonomous = subscription.autonomous_trading_enabled
    subscription.is_active = False
    subscription.autonomous_trading_enabled = False
    subscription.autonomy_enabled_by = None
    subscription.autonomy_enabled_at = None
    return was_autonomous
