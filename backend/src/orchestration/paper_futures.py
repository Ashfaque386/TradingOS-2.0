"""Monthly futures in paper trading: picking the contract and rolling it (real-market-data plan,
phase 4).

A futures subscription reads its signals from the underlying (NIFTY, RELIANCE) and trades one
monthly contract. Contracts expire, so the subscription is *rolled*: a few days before expiry the
position is closed on the old contract and the subscription is pointed at the next one; the signal
logic then re-enters there on its own (flat plus a standing signal is an entry), in whole lots of
the **new** contract's lot size -- exchanges revise lot sizes between contracts, so the figure is
read from the broker's instrument master at every roll, never kept.

Rules
-----
- The contract is the soonest one expiring **more than `roll_days` days from today**, so a
  subscription is never put into a contract that is about to be rolled out of again.
- A roll closes an open position the way any manual close does: through the risk gate and kill
  switch, at a fresh live tick, walking the real depth, charged its costs. A roll that cannot do
  that now (no fresh tick or book, a refusal, a partial fill) is **postponed**, not forced: the
  subscription stays on the old contract, with the position, until a later run succeeds. It is
  never switched to a new contract while still holding the old one.
- A contract past its expiry that was not rolled shows as `halted_no_data` with the reason
  (`paper_subscription_controls.contract_problem`).
- Not modelled yet (phase 5, with options): squaring a position off at expiry when the roll could
  not run, and any delivery/settlement mechanics.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.base import BrokerAdapter, FuturesContract
from src.brokers.futures_contracts import (
    NoFuturesContractError,
    resolve_contract,
    roll_due,
)
from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.order_book import OrderBookProvider
from src.engine.paper_trading.tick_feed import get_latest_tick_id
from src.engine.risk.compliance import RegulatoryDataProvider
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_subscription_controls import (
    CloseBlockedError,
    PriceUnavailableError,
    SubscriptionStateError,
    close_position,
)
from src.orchestration.paper_trading import _get_or_create_position

logger = structlog.get_logger(__name__)

ROLL_ACTOR = "system:futures-roll"

RollStatus = Literal["rolled", "postponed", "not_due", "no_contract"]


@dataclass(frozen=True, slots=True)
class RollOutcome:
    subscription_id: str
    status: RollStatus
    old_contract: str | None
    new_contract: str | None = None
    detail: str = ""


async def roll_subscription(
    db: AsyncSession,
    redis: Redis,
    subscription: PaperTradingSubscription,
    contract: FuturesContract,
    *,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
) -> RollOutcome:
    """Moves `subscription` onto `contract`, closing its open position on the old one first."""
    old = subscription.contract_symbol
    position = await _get_or_create_position(db, subscription)
    if position.quantity != 0:
        try:
            closed = await close_position(
                db,
                redis,
                subscription.id,
                actor=ROLL_ACTOR,
                reason=f"rolling {old} into {contract.symbol} before expiry",
                price=None,
                administrative=False,
                order_book_provider=order_book_provider,
                regulatory_provider=regulatory_provider,
            )
        except (CloseBlockedError, PriceUnavailableError, SubscriptionStateError) as exc:
            logger.warning(
                "paper_futures.roll_postponed",
                subscription_id=str(subscription.id),
                contract=old,
                reason=str(exc),
            )
            return RollOutcome(str(subscription.id), "postponed", old, contract.symbol, str(exc))
        if closed.remaining_quantity != 0:
            reason = f"only part of the position closed ({closed.remaining_quantity} left)"
            logger.warning(
                "paper_futures.roll_postponed",
                subscription_id=str(subscription.id),
                contract=old,
                reason=reason,
            )
            return RollOutcome(str(subscription.id), "postponed", old, contract.symbol, reason)
        await db.refresh(subscription)

    subscription.contract_symbol = contract.symbol
    subscription.contract_expiry = contract.expiry
    subscription.lot_size = contract.lot_size
    # The new contract's stream starts empty or is already running; either way, nothing that was
    # published before this moment is this subscription's to act on.
    subscription.tick_cursor = await get_latest_tick_id(redis, contract.symbol) or "0"
    position.symbol = contract.symbol
    await write_audit_entry(
        db,
        actor=ROLL_ACTOR,
        action="paper_trading.futures_rolled",
        entity_type="paper_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "underlying": subscription.symbol,
            "from": old,
            "to": contract.symbol,
            "expiry": contract.expiry.isoformat(),
            "lot_size": contract.lot_size,
        },
    )
    await db.commit()
    logger.info(
        "paper_futures.rolled",
        subscription_id=str(subscription.id),
        old_contract=old,
        new_contract=contract.symbol,
        lot_size=contract.lot_size,
    )
    return RollOutcome(str(subscription.id), "rolled", old, contract.symbol)


async def run_futures_roll(
    db: AsyncSession,
    redis: Redis,
    *,
    adapters: Sequence[BrokerAdapter],
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    roll_days: int,
    now: datetime | None = None,
) -> list[RollOutcome]:
    """Rolls every active futures subscription whose contract is due. Never raises for one
    subscription's problem: each outcome says what happened, the rest are still handled."""
    today = (now or datetime.now(UTC)).astimezone(IST).date()
    subscriptions = (
        (
            await db.execute(
                select(PaperTradingSubscription).where(
                    PaperTradingSubscription.is_active.is_(True),
                    PaperTradingSubscription.instrument_kind == "future",
                )
            )
        )
        .scalars()
        .all()
    )
    outcomes: list[RollOutcome] = []
    for subscription in subscriptions:
        expiry = subscription.contract_expiry
        if expiry is None or not roll_due(expiry, today=today, roll_days=roll_days):
            continue
        try:
            contract = await resolve_contract(
                adapters, subscription.symbol, today=today, roll_days=roll_days
            )
        except NoFuturesContractError as exc:
            logger.warning(
                "paper_futures.no_contract_to_roll_into",
                subscription_id=str(subscription.id),
                contract=subscription.contract_symbol,
                reason=str(exc),
            )
            outcomes.append(
                RollOutcome(
                    str(subscription.id),
                    "no_contract",
                    subscription.contract_symbol,
                    None,
                    str(exc),
                )
            )
            continue
        if contract.symbol == subscription.contract_symbol:
            continue  # already on the right contract (a stale due-date, or a lot-size refresh)
        outcomes.append(
            await roll_subscription(
                db,
                redis,
                subscription,
                contract,
                order_book_provider=order_book_provider,
                regulatory_provider=regulatory_provider,
            )
        )
    return outcomes
