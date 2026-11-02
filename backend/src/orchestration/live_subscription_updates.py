"""Reading, saving and auditing a change to a live subscription's limits, or switching it off.

The rules (which way a limit may move, what protects an open position) are
`src.orchestration.live_subscription_limits`; this module loads the subscription and what the
strategy holds, applies the rules, saves, and writes the audit entry with the reason and the values
before and after.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.orchestration.live_subscription_limits import (
    LimitChange,
    plan_deactivation,
    plan_limit_changes,
    switch_off,
)
from src.orchestration.live_trading import SubscriptionNotFoundError


async def _held(db: AsyncSession, subscription: LiveTradingSubscription) -> tuple[int, float]:
    """What the strategy holds on this symbol: shares (negative when short) and what they cost."""
    position = (
        await db.execute(
            select(LivePosition).where(
                LivePosition.strategy_id == subscription.strategy_id,
                LivePosition.symbol == subscription.symbol,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        return 0, 0.0
    return position.quantity, abs(position.quantity) * position.avg_cost


async def tighten_limits(
    db: AsyncSession,
    subscription_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    requested: dict[str, float | int],
) -> tuple[LiveTradingSubscription, list[LimitChange]]:
    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None:
        raise SubscriptionNotFoundError(subscription_id)
    quantity, cost = await _held(db, subscription)
    changes = plan_limit_changes(subscription, requested, held_quantity=quantity, held_cost=cost)
    for change in changes:
        setattr(subscription, change.field, change.after)
    await write_audit_entry(
        db,
        actor=actor,
        action="live_trading_subscription.limits_tightened",
        entity_type="live_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "strategy_id": str(subscription.strategy_id),
            "symbol": subscription.symbol,
            "reason": reason,
            "changes": {c.field: {"before": c.before, "after": c.after} for c in changes},
            "held_quantity": quantity,
        },
    )
    await db.commit()
    await db.refresh(subscription)
    return subscription, changes


async def deactivate_subscription(
    db: AsyncSession, subscription_id: uuid.UUID, *, actor: str, reason: str
) -> LiveTradingSubscription:
    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None:
        raise SubscriptionNotFoundError(subscription_id)
    quantity, _cost = await _held(db, subscription)
    plan_deactivation(subscription, held_quantity=quantity)
    was_autonomous = switch_off(subscription)
    await write_audit_entry(
        db,
        actor=actor,
        action="live_trading_subscription.deactivated",
        entity_type="live_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "strategy_id": str(subscription.strategy_id),
            "symbol": subscription.symbol,
            "reason": reason,
            "autonomy_was_on": was_autonomous,
        },
    )
    await db.commit()
    await db.refresh(subscription)
    return subscription
