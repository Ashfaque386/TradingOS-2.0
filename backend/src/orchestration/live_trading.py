"""LiveExecutionPipeline (Build Spec §12): the human-gated live trading
flow that operationalizes this rebuild's single most important product
decision -- full autonomy through paper trading, mandatory human
validation for anything that touches real money.

**State machine, and which function may perform which transition** (see
`src.models.live_order_intent.LiveOrderIntent` for the column set, taken
verbatim from Build Spec §12.3):

    pending_approval --[generate_live_order_intent, batch-eligible]--> approved
    pending_approval --[approve_live_order_intent]-----------------> approved
    pending_approval --[reject_live_order_intent]-------------------> rejected
    pending_approval --[expire_stale_intents sweep]------------------> expired
    approved         --[expire_stale_intents sweep, unsubmitted]-----> expired
    approved         --[_submit_intent_to_broker, success]-----------> submitted
    approved         --[_submit_intent_to_broker, failure]-----------> failed

No function anywhere in this module ever writes `submitted` except
`_submit_intent_to_broker`, and nothing calls that function except an
explicit human approval (`approve_live_order_intent`) or an
already-logged advance human authorization consumed inside
`generate_live_order_intent`'s batch path -- `generate_live_order_intent`
itself only ever writes `pending_approval` or (batch case) `approved`,
never `submitted`. That is what "never auto-submitted" means concretely
here.

**Kill switch stops generation, not just approval** (Build Spec §12.2):
`generate_live_order_intent` calls `assert_not_tripped(db, "live")`
*first*, before it even looks up the subscription -- a tripped switch
means no `LiveOrderIntent` row is created at all, not merely that one
would later be rejected. `_submit_intent_to_broker` re-checks the same
switch (via `create_order_intent`) immediately before every broker call
too, since a human can sit on a pending intent for up to 5 minutes and
the switch might trip in that window -- generation-time blocking is
additive to that existing submission-time check, never a replacement
for it.

**Signal logic is the same tick-driven logic as the paper engine**
(Phase 7): `generate_builtin_signals`/`detect_todays_signal` for the
daily layer, `check_universal_stop_loss` for the intraday layer, and
`apply_fill`/`Position` for position accounting -- all imported directly
from `src.engine.backtest`/`src.engine.paper_trading`, never
reimplemented. The one deliberate difference from the paper engine: a
tick that would trigger an entry/exit/stop never calls
`apply_fill`/places an order by itself -- it only ever creates a
`pending_approval` `LiveOrderIntent` row. Position accounting
(`LivePosition`) only advances once a fill is actually *submitted* to
the broker (`_submit_intent_to_broker`), using the price re-quoted at
submission time -- see `Trade`'s and `LivePosition`'s own docstrings for
why that price is honestly "what we told the broker," not a confirmed
execution price.

**Bounded batch pre-authorization** (Build Spec §12.2):
`_find_eligible_batch_authorization` is the one place that decides
whether an intent may skip the human-click step -- it re-checks
`intents_used < max_intents` and `notional <= max_notional_per_intent`
itself, under `SELECT ... FOR UPDATE`, every single time, rather than
trusting anything a caller supplies. There is no API surface anywhere in
this codebase that lets a caller pass a `batch_authorization_id` and
have it trusted without this same re-check -- see
`tests/test_orchestration_live_trading.py` for a client that tries to
exceed both bounds and is refused.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pandas as pd
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.base import BrokerAdapter, OrderRequest, OrderSide, OrderType
from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.signals import BuiltinStrategy, generate_builtin_signals
from src.engine.paper_trading.daily_signal import detect_todays_signal
from src.engine.paper_trading.position_ledger import Position, apply_fill
from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.paper_trading.stop_loss import check_universal_stop_loss
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.live_batch_authorization import LiveBatchAuthorization
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.order import Order
from src.models.strategy import Strategy, StrategyStatus
from src.models.trade import Trade
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel
from src.orchestration.kill_switch import assert_not_tripped
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

logger = structlog.get_logger(__name__)

DAILY_LOOKBACK_DAYS = 90
DEFAULT_INTENT_EXPIRY_SECONDS = 90
# Build Spec §12.2's hard platform-wide maximum -- 5 minutes. Enforced
# both here (Python-level, at enrollment) and in the DB CheckConstraint
# on live_trading_subscriptions.intent_expiry_seconds.
MAX_INTENT_EXPIRY_SECONDS = 300

_LIVE_ELIGIBLE_STATUSES = (StrategyStatus.LIVE_ELIGIBLE.value, StrategyStatus.LIVE.value)

__all__ = [
    "IntentExpiredError",
    "IntentExpiryWindowTooLongError",
    "IntentNotFoundError",
    "IntentNotPendingError",
    "NoBrokerConfiguredError",
    "NoLiveSubscriptionError",
    "StrategyNotLiveEligibleError",
    "approve_live_order_intent",
    "create_batch_authorization",
    "enroll_in_live_trading",
    "expire_stale_intents",
    "generate_live_order_intent",
    "reconcile_pending_trades",
    "reject_live_order_intent",
    "run_live_daily_signal_generation",
]


class StrategyNotLiveEligibleError(Exception):
    def __init__(self, strategy_id: uuid.UUID):
        self.strategy_id = strategy_id
        super().__init__(
            f"strategy {strategy_id} is not LiveEligible/Live -- the one-time human "
            "sign-off (Build Spec §12.1) has not been granted"
        )


class IntentExpiryWindowTooLongError(ValueError):
    def __init__(self, intent_expiry_seconds: int):
        self.intent_expiry_seconds = intent_expiry_seconds
        super().__init__(
            f"intent_expiry_seconds={intent_expiry_seconds} exceeds the hard "
            f"platform-wide maximum of {MAX_INTENT_EXPIRY_SECONDS} seconds "
            "(Build Spec §12.2)"
        )


class IntentNotFoundError(Exception):
    def __init__(self, intent_id: uuid.UUID):
        self.intent_id = intent_id
        super().__init__(f"live order intent {intent_id} not found")


class IntentNotPendingError(Exception):
    def __init__(self, intent_id: uuid.UUID, status: str):
        self.intent_id = intent_id
        self.status = status
        super().__init__(f"live order intent {intent_id} is {status!r}, not pending_approval")


class IntentExpiredError(Exception):
    def __init__(self, intent_id: uuid.UUID):
        self.intent_id = intent_id
        super().__init__(f"live order intent {intent_id} has expired")


class NoBrokerConfiguredError(Exception):
    def __init__(self):
        super().__init__("no broker adapter configured -- cannot submit a live order")


class NoLiveSubscriptionError(Exception):
    def __init__(self, strategy_id: uuid.UUID, symbol: str):
        self.strategy_id = strategy_id
        self.symbol = symbol
        super().__init__(f"no live trading subscription for strategy {strategy_id} / {symbol}")


async def enroll_in_live_trading(
    db: AsyncSession,
    *,
    strategy_id: uuid.UUID,
    symbol: str,
    broker_name: str,
    builtin_strategy: BuiltinStrategy = "sma_crossover",
    sma_window: int = 15,
    initial_capital: float = 100_000.0,
    stop_loss_pct: float = 3.0,
    position_size_pct: float = 5.0,
    intent_expiry_seconds: int = DEFAULT_INTENT_EXPIRY_SECONDS,
    created_by: str | None = None,
) -> LiveTradingSubscription:
    if not (0 < intent_expiry_seconds <= MAX_INTENT_EXPIRY_SECONDS):
        raise IntentExpiryWindowTooLongError(intent_expiry_seconds)

    strategy = await db.get(Strategy, strategy_id)
    if strategy is None or strategy.status not in _LIVE_ELIGIBLE_STATUSES:
        raise StrategyNotLiveEligibleError(strategy_id)

    subscription = LiveTradingSubscription(
        strategy_id=strategy_id,
        symbol=symbol,
        broker_name=broker_name,
        builtin_strategy=builtin_strategy,
        sma_window=sma_window,
        initial_capital=initial_capital,
        stop_loss_pct=stop_loss_pct,
        position_size_pct=position_size_pct,
        intent_expiry_seconds=intent_expiry_seconds,
        created_by=created_by,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def run_live_daily_signal_generation(
    db: AsyncSession,
    *,
    price_provider: PriceDataProvider,
    as_of: pd.Timestamp,
) -> list[LiveTradingSubscription]:
    result = await db.execute(
        select(LiveTradingSubscription).where(LiveTradingSubscription.is_active.is_(True))
    )
    subscriptions = result.scalars().all()

    updated: list[LiveTradingSubscription] = []
    for subscription in subscriptions:
        strategy = await db.get(Strategy, subscription.strategy_id)
        if strategy is None or strategy.status not in _LIVE_ELIGIBLE_STATUSES:
            # The one-time sign-off enforced here too, not only at
            # enrollment -- a strategy demoted after enrollment (not a
            # modeled flow today, but defended against anyway) must never
            # keep generating live signals.
            continue

        bars = price_provider.daily_bars(
            subscription.symbol, as_of=pd.Timestamp(as_of), lookback_days=DAILY_LOOKBACK_DAYS
        )
        signals = generate_builtin_signals(
            bars, subscription.builtin_strategy, subscription.sma_window
        )
        # Same posture as Phase 7's daily job: the real Phase 5 engine
        # runs here too so a genuinely failing backtest surfaces, but its
        # result isn't itself the signal source -- detect_todays_signal
        # reads the same position-lag rule the engine uses internally.
        run_vectorized_backtest(bars, signals)

        decision = detect_todays_signal(signals)
        if decision.signal is None:
            continue

        subscription.last_signal_type = decision.signal
        subscription.last_signal_reference_price = float(bars["close"].iloc[-1])
        subscription.last_signal_generated_at = datetime.now(UTC)
        updated.append(subscription)

    if updated:
        await db.commit()
        for subscription in updated:
            await db.refresh(subscription)
    return updated


async def _get_or_create_live_position(
    db: AsyncSession, *, strategy_id: uuid.UUID, symbol: str
) -> LivePosition:
    result = await db.execute(select(LivePosition).where(LivePosition.strategy_id == strategy_id))
    position = result.scalar_one_or_none()
    if position is None:
        position = LivePosition(strategy_id=strategy_id, symbol=symbol, quantity=0, avg_cost=0.0)
        db.add(position)
        await db.commit()
        await db.refresh(position)
    return position


async def _find_eligible_batch_authorization(
    db: AsyncSession, *, strategy_id: uuid.UUID, notional: float, now: datetime
) -> LiveBatchAuthorization | None:
    """The server-side enforcement point (Build Spec §12.2): re-checks
    the count and notional bounds itself, under `FOR UPDATE`, every call
    -- never trusts a caller's claim that a batch authorization applies.
    """
    result = await db.execute(
        select(LiveBatchAuthorization)
        .where(
            LiveBatchAuthorization.strategy_id == strategy_id,
            LiveBatchAuthorization.window_start <= now,
            LiveBatchAuthorization.window_end >= now,
            LiveBatchAuthorization.intents_used < LiveBatchAuthorization.max_intents,
            LiveBatchAuthorization.max_notional_per_intent >= notional,
        )
        .order_by(LiveBatchAuthorization.created_at)
        .limit(1)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def create_batch_authorization(
    db: AsyncSession,
    *,
    strategy_id: uuid.UUID,
    authorized_by: str,
    max_intents: int,
    max_notional_per_intent: float,
    window_start: datetime,
    window_end: datetime,
) -> LiveBatchAuthorization:
    if not authorized_by:
        raise ValueError("create_batch_authorization requires an explicit authorized_by actor")
    if max_intents <= 0:
        raise ValueError("max_intents must be positive")
    if max_notional_per_intent <= 0:
        raise ValueError("max_notional_per_intent must be positive")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    authorization = LiveBatchAuthorization(
        strategy_id=strategy_id,
        authorized_by=authorized_by,
        max_intents=max_intents,
        max_notional_per_intent=max_notional_per_intent,
        window_start=window_start,
        window_end=window_end,
    )
    db.add(authorization)
    await write_audit_entry(
        db,
        actor=authorized_by,
        action="live_batch_authorization.created",
        entity_type="strategy",
        entity_id=str(strategy_id),
        details={
            "max_intents": max_intents,
            "max_notional_per_intent": max_notional_per_intent,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    )
    await db.commit()
    await db.refresh(authorization)
    return authorization


async def generate_live_order_intent(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    tick_price: float,
    adapter: BrokerAdapter | None = None,
    regulatory_provider: RegulatoryDataProvider | None = None,
) -> LiveOrderIntent | None:
    """Returns the `LiveOrderIntent` this tick produced, or `None` if the
    tick triggered no action. Raises `KillSwitchTrippedError` (uncaught)
    when the live kill switch is tripped -- callers (the intraday
    scheduler job) must catch this per-tick, the same posture as Phase
    7's `process_tick`/tick-drain job, so one tripped switch doesn't
    crash the whole drain loop for every other subscription too."""
    await assert_not_tripped(db, "live")

    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None or not subscription.is_active:
        return None

    strategy = await db.get(Strategy, subscription.strategy_id)
    if strategy is None or strategy.status not in _LIVE_ELIGIBLE_STATUSES:
        return None

    position = await _get_or_create_live_position(
        db, strategy_id=subscription.strategy_id, symbol=subscription.symbol
    )

    stop = check_universal_stop_loss(
        position_qty=position.quantity,
        avg_cost=position.avg_cost,
        tick_price=tick_price,
        stop_loss_pct=subscription.stop_loss_pct,
    )

    side: str | None = None
    quantity = 0
    intent_type: str | None = None

    if stop.triggered:
        side = "sell" if position.quantity > 0 else "buy"
        quantity = abs(position.quantity)
        intent_type = "stop"
    elif subscription.last_signal_type is not None:
        if position.quantity > 0 and subscription.last_signal_type == "SELL":
            side = "sell"
            quantity = position.quantity
            intent_type = "exit"
        elif position.quantity == 0 and subscription.last_signal_type == "BUY":
            side = "buy"
            entry_capital = subscription.initial_capital * (subscription.position_size_pct / 100.0)
            quantity = int(entry_capital // tick_price)
            intent_type = "entry"

    if side is None or quantity <= 0 or intent_type is None:
        return None

    now = datetime.now(UTC)
    notional = quantity * tick_price

    intent = LiveOrderIntent(
        strategy_id=strategy.id,
        symbol=subscription.symbol,
        side=side,
        quantity=quantity,
        intent_type=intent_type,
        expires_at=now + timedelta(seconds=subscription.intent_expiry_seconds),
        status="pending_approval",
    )

    batch = await _find_eligible_batch_authorization(
        db, strategy_id=strategy.id, notional=notional, now=now
    )
    if batch is not None:
        intent.status = "approved"
        intent.approved_by = f"batch:{batch.authorized_by}"
        intent.approved_at = now
        intent.batch_authorization_id = batch.id
        batch.intents_used += 1
        db.add(batch)

    db.add(intent)
    await db.commit()
    await db.refresh(intent)

    if batch is None:
        # Build Spec §18's explicit "sign-off items -- including live
        # order intents from Phase 9" -- only the genuinely-pending case;
        # a batch-consumed intent below skips straight to approved and
        # was never a human sign-off item in the first place.
        await notify(
            AlertLevel.SIGN_OFF,
            title=f"Live order intent pending approval: {intent.symbol}",
            body=f"{intent.side.upper()} {intent.quantity} {intent.symbol} ({intent.intent_type}), "
            f"expires {intent.expires_at.isoformat()}",
            details={"intent_id": str(intent.id), "strategy_id": str(strategy.id)},
        )
        return intent

    if adapter is None or regulatory_provider is None:
        # Pre-authorized but nothing can submit it right now -- left
        # 'approved' rather than forced back to pending_approval (the
        # authorization was already consumed and logged); the expiry
        # sweep resolves it to 'expired' safely if it's never submitted
        # within its window, same as any other unsubmitted intent.
        logger.warning(
            "live_trading.batch_approved_intent_cannot_submit",
            intent_id=str(intent.id),
            strategy_id=str(strategy.id),
        )
        return intent

    return await _submit_intent_to_broker(
        db, intent, adapter=adapter, regulatory_provider=regulatory_provider
    )


async def _fail_intent(
    db: AsyncSession, intent: LiveOrderIntent, *, reason: str
) -> LiveOrderIntent:
    intent.status = "failed"
    await write_audit_entry(
        db,
        actor=intent.approved_by or "system",
        action="live_order_intent.submission_failed",
        entity_type="live_order_intent",
        entity_id=str(intent.id),
        details={"reason": reason},
    )
    await db.commit()
    await db.refresh(intent)
    return intent


async def _submit_intent_to_broker(
    db: AsyncSession,
    intent: LiveOrderIntent,
    *,
    adapter: BrokerAdapter,
    regulatory_provider: RegulatoryDataProvider,
) -> LiveOrderIntent:
    """The one function in this codebase that ever turns an `approved`
    intent into a real broker call -- shared by `approve_live_order_intent`
    (a human clicking Approve) and `generate_live_order_intent`'s batch
    auto-approval path (an already-logged advance human authorization).
    Re-quotes the symbol right before submission rather than trusting a
    possibly-stale generation-time price -- a real market order is priced
    at execution, not at planning time, and `live_order_intents` has no
    price column of its own (Build Spec §12.3's schema is exact)."""
    try:
        quote = await adapter.get_quote(intent.symbol)
    except Exception as exc:  # noqa: BLE001 - any quote failure is an honest submission failure
        return await _fail_intent(db, intent, reason=f"quote lookup failed: {exc}")

    reference_price = quote.last_price
    subscription = await _get_live_subscription(
        db, strategy_id=intent.strategy_id, symbol=intent.symbol
    )

    try:
        await create_order_intent(
            db,
            mode="live",
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            proposed_price=reference_price,
            reference_price=reference_price,
            proposed_position_value=intent.quantity * reference_price,
            portfolio_value=subscription.initial_capital,
            regulatory_provider=regulatory_provider,
        )
    except KillSwitchTrippedError as exc:
        logger.warning(
            "live_trading.submission_blocked_by_kill_switch",
            intent_id=str(intent.id),
            reason=exc.reason,
        )
        return await _fail_intent(db, intent, reason=f"kill switch tripped: {exc.reason}")
    except OrderIntentRejectedError as exc:
        return await _fail_intent(db, intent, reason="; ".join(exc.reasons))

    try:
        result = await adapter.place_order(
            OrderRequest(
                symbol=intent.symbol,
                side=OrderSide(intent.side),
                quantity=intent.quantity,
                order_type=OrderType.MARKET,
            )
        )
    except Exception as exc:  # noqa: BLE001 - BrokerServerError/BrokerRequestError/CircuitOpenError/network errors all honestly fail the intent
        return await _fail_intent(db, intent, reason=f"broker submission failed: {exc}")

    order = Order(
        live_order_intent_id=intent.id,
        strategy_id=intent.strategy_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        broker_name=adapter.broker_name,
        broker_order_id=result.broker_order_id,
        status="submitted",
    )
    db.add(order)
    await db.flush()

    db.add(
        Trade(
            order_id=order.id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=reference_price,
            status="pending_confirmation",
        )
    )

    position = await _get_or_create_live_position(
        db, strategy_id=intent.strategy_id, symbol=intent.symbol
    )
    application = apply_fill(
        Position(quantity=position.quantity, avg_cost=position.avg_cost),
        side=intent.side,
        fill_quantity=intent.quantity,
        fill_price=reference_price,
    )
    position.quantity = application.new_position.quantity
    position.avg_cost = application.new_position.avg_cost
    position.realized_pnl += application.realized_pnl

    intent.status = "submitted"
    intent.resulting_order_id = order.id

    await write_audit_entry(
        db,
        actor=intent.approved_by or "system",
        action="live_order_intent.submitted",
        entity_type="live_order_intent",
        entity_id=str(intent.id),
        details={
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "broker_name": adapter.broker_name,
            "broker_order_id": result.broker_order_id,
            "reference_price": reference_price,
        },
    )

    await db.commit()
    await db.refresh(intent)
    return intent


async def _get_live_subscription(
    db: AsyncSession, *, strategy_id: uuid.UUID, symbol: str
) -> LiveTradingSubscription:
    result = await db.execute(
        select(LiveTradingSubscription).where(
            LiveTradingSubscription.strategy_id == strategy_id,
            LiveTradingSubscription.symbol == symbol,
        )
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        raise NoLiveSubscriptionError(strategy_id, symbol)
    return subscription


async def approve_live_order_intent(
    db: AsyncSession,
    intent_id: uuid.UUID,
    *,
    approved_by: str,
    adapter: BrokerAdapter | None,
    regulatory_provider: RegulatoryDataProvider,
) -> LiveOrderIntent:
    if not approved_by:
        raise ValueError("approve_live_order_intent requires an explicit approved_by actor")

    intent = await db.get(LiveOrderIntent, intent_id)
    if intent is None:
        raise IntentNotFoundError(intent_id)
    if intent.status != "pending_approval":
        raise IntentNotPendingError(intent_id, intent.status)

    now = datetime.now(UTC)
    if intent.expires_at <= now:
        intent.status = "expired"
        await db.commit()
        raise IntentExpiredError(intent_id)

    if adapter is None:
        raise NoBrokerConfiguredError()

    intent.status = "approved"
    intent.approved_by = approved_by
    intent.approved_at = now
    await db.commit()
    await db.refresh(intent)

    return await _submit_intent_to_broker(
        db, intent, adapter=adapter, regulatory_provider=regulatory_provider
    )


async def reject_live_order_intent(
    db: AsyncSession, intent_id: uuid.UUID, *, rejected_by: str
) -> LiveOrderIntent:
    """Never touches the broker adapter, under any circumstance --
    a rejected intent has no path to submission anywhere in this module."""
    if not rejected_by:
        raise ValueError("reject_live_order_intent requires an explicit rejected_by actor")

    intent = await db.get(LiveOrderIntent, intent_id)
    if intent is None:
        raise IntentNotFoundError(intent_id)
    if intent.status != "pending_approval":
        raise IntentNotPendingError(intent_id, intent.status)

    intent.status = "rejected"
    await write_audit_entry(
        db,
        actor=rejected_by,
        action="live_order_intent.rejected",
        entity_type="live_order_intent",
        entity_id=str(intent.id),
        details={"symbol": intent.symbol, "side": intent.side, "quantity": intent.quantity},
    )
    await db.commit()
    await db.refresh(intent)
    return intent


async def expire_stale_intents(db: AsyncSession) -> int:
    """The scheduled sweep (Build Spec §12.2): every `pending_approval`
    or unsubmitted `approved` intent past its `expires_at` resolves to
    `expired` -- safely, with no order ever submitted. This must be
    correct even if no human is looking at the sign-off queue UI at all,
    which is exactly why this is a DB-level bulk UPDATE driven by
    `src.orchestration.live_trading_scheduler`'s own timer, never a
    client-side countdown that silently does nothing if the browser tab
    is closed."""
    now = datetime.now(UTC)
    stmt = (
        update(LiveOrderIntent)
        .where(
            LiveOrderIntent.status.in_(("pending_approval", "approved")),
            LiveOrderIntent.expires_at <= now,
        )
        .values(status="expired")
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


def _normalize_broker_order_status(raw_status: str) -> str | None:
    """Translates a broker's own order-status vocabulary (passed through
    unnormalized on `BrokerOrder.status` -- see
    `src.brokers.base.BrokerAdapter.get_order_book`'s docstring; Zerodha
    reports e.g. `"COMPLETE"`/`"REJECTED"`/`"OPEN"`, Upstox reports
    `"complete"`/`"rejected"`/`"open"`) into this codebase's own terminal
    `Trade.status` vocabulary. Returns `None` for anything not yet a
    terminal outcome (open, trigger-pending, etc.) -- such a trade is left
    at `pending_confirmation` for the next reconciliation pass to re-check,
    exactly like a still-open order really is still unresolved."""
    status = raw_status.strip().upper()
    if status == "COMPLETE":
        return "filled"
    if status == "REJECTED":
        return "rejected"
    if status == "CANCELLED":
        return "cancelled"
    return None


async def reconcile_pending_trades(db: AsyncSession, adapter: BrokerAdapter) -> int:
    """The broker-fill reconciliation job (Phase 16 audit follow-up B):
    closes the gap where `Trade.status` could never progress past
    `pending_confirmation` because nothing ever polled the broker for a
    real outcome. `Trade` carries no broker order id of its own, so this
    joins through `Order.broker_order_id`/`Order.broker_name`; the adapter
    layer exposes no per-order status lookup (only the bulk
    `get_order_book()`), so this fetches that once and matches client-side
    rather than doing one round-trip per pending trade. Only trades routed
    through `adapter.broker_name` are considered -- this codebase only
    ever has one broker configured at a time (`build_configured_adapter`),
    so a trade left over from a previously-configured, now-unconfigured
    broker has no adapter to reconcile it against and is correctly left
    pending rather than guessed at.

    Runs unconditionally, not gated by `is_market_open_ist()` -- like
    `expire_stale_intents`, a fill can be confirmed by the broker shortly
    after the market's new-order window closes, and this must keep
    resolving those regardless of whether anyone is looking at the UI.
    Safe to call repeatedly: a broker-lookup failure or an order not yet
    appearing in the book leaves the trade untouched for the next pass,
    never raises out of this function, and never partially commits (one
    commit for the whole batch)."""
    pending = (
        await db.execute(
            select(Trade, Order.broker_order_id)
            .join(Order, Trade.order_id == Order.id)
            .where(Trade.status == "pending_confirmation", Order.broker_name == adapter.broker_name)
        )
    ).all()
    if not pending:
        return 0

    try:
        broker_orders = await adapter.get_order_book()
    except Exception:
        logger.exception(
            "live_trading.reconciliation_order_book_fetch_failed", broker=adapter.broker_name
        )
        return 0

    by_broker_order_id = {bo.broker_order_id: bo for bo in broker_orders}

    now = datetime.now(UTC)
    reconciled = 0
    for trade, broker_order_id in pending:
        if not broker_order_id:
            continue
        broker_order = by_broker_order_id.get(broker_order_id)
        if broker_order is None:
            continue
        outcome = _normalize_broker_order_status(broker_order.status)
        if outcome is None:
            continue

        trade.status = outcome
        trade.confirmed_at = now
        if outcome == "filled" and broker_order.average_price is not None:
            trade.fill_price = broker_order.average_price

        await write_audit_entry(
            db,
            actor="system",
            action="trade.reconciled",
            entity_type="trade",
            entity_id=str(trade.id),
            details={
                "symbol": trade.symbol,
                "broker_name": adapter.broker_name,
                "broker_order_id": broker_order_id,
                "outcome": outcome,
                "broker_status": broker_order.status,
                "fill_price": broker_order.average_price,
            },
        )
        reconciled += 1

    if reconciled:
        await db.commit()
    return reconciled
