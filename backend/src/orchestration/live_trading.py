"""LiveExecutionPipeline (Build Spec §12, redesigned by Phase 18): fully
autonomous live trading once an operator has explicitly enabled it, with
the deterministic safety layer -- never a human clicking Approve -- as
the only thing standing between a signal and a real broker order. See
Non-Negotiable Rule #1 (CLAUDE.md) for the precise, current statement of
this design; this docstring is the implementation-level detail behind it.

**Why this changed**: the original Build Spec §12 required a human to
approve every single live order intent, with a configurable expiry
window (default 90s, max 5 minutes) as the only thing that happened if
nobody acted. Phase 18 removes that gate entirely and replaces it with
three things that must ALL pass before an order reaches a broker: the
master autonomy switch (Part 2), the Kill Switch (unchanged), and a
standing, always-on per-subscription rate/notional cap (Part 3) -- see
`generate_live_order_intent`'s own body for the exact order these are
checked in and why.

**State machine, and which function may perform which transition** (see
`src.models.live_order_intent.LiveOrderIntent` for the full column set
and status vocabulary):

    (no signal / autonomy off / not live-eligible) --> no row at all
    (signal, standing cap exceeded)        --[generate_live_order_intent]--> capped (terminal)
    (signal, within caps, kill switch off) --[generate_live_order_intent]--> generated
    generated --[_submit_intent_to_broker, same call, success]--------------> submitted (terminal)
    generated --[_submit_intent_to_broker, same call, failure]--------------> failed (terminal)
    generated --[no broker adapter configured -- rare]-----------------------> (left generated)
    generated --[expire_stale_intents sweep, still unsubmitted]-------------> expired (terminal)

Submission is synchronous, in the *same* `generate_live_order_intent`
call that created the `generated` row -- there is no separate function
anywhere in this codebase that later submits an already-`generated`
intent (the old `approve_live_order_intent` did that; it's gone). The
only way a `generated` row outlives its own generation call is the rare
case no broker adapter was configured at generation time; the expiry
sweep is what eventually resolves that one honestly, as `expired`, never
as a late, out-of-band submission.

**Check order inside `generate_live_order_intent`, deliberate and never
reordered**:
1. The subscription must exist, be active, and its strategy must still
   be live-eligible -- pure existence preconditions, not safety checks,
   so they run first regardless.
2. **The master autonomy switch** (`subscription.autonomous_trading_enabled`)
   -- checked before anything else that could actually block or allow a
   trade, per Non-Negotiable Rule #1: a subscription with autonomy
   disabled produces no `LiveOrderIntent` row at all, full stop, not even
   a rejected one. This is checked before the Kill Switch specifically so
   that a strategy nobody has deliberately opted in stays completely
   silent regardless of kill-switch state -- there is nothing to protect
   against for a strategy that was never authorized to trade in the first
   place.
3. **The Kill Switch** (`assert_not_tripped(db, "live")`) -- unchanged
   from the original design: a tripped switch blocks intent *generation*
   itself, raising `KillSwitchTrippedError` (uncaught here; callers like
   the intraday scheduler job catch it per-tick so one tripped switch
   doesn't kill the drain loop for every other subscription).
4. Signal computation (unchanged from Phase 9) -- if the tick produces no
   actionable entry/exit/stop, nothing is written.
5. **The standing rate/notional cap** (Part 3) -- a rolling-window count
   of this strategy's own recent real intents
   (`generated`/`submitted`/`failed`/`expired`, i.e. every case where a
   real order was actually attempted -- `capped` rows don't count towards
   their own cap) against `max_intents_per_window`, plus the candidate
   order's notional against `max_notional_per_intent`. Exceeding either
   writes a `capped` row (logged via both an audit entry and a structlog
   warning -- never silently dropped) and returns without ever reaching
   `_submit_intent_to_broker`. This is what stops a misconfigured
   strategy firing on every tick from doing unbounded damage,
   independent of and in addition to the Kill Switch.
6. Only after all four gates pass does `_submit_intent_to_broker` run,
   which re-checks the Kill Switch *again* (via `create_order_intent`,
   unchanged) immediately before the broker call -- the only meaningful
   window left for a kill-switch trip to matter is the few milliseconds
   inside this one function call, not the multi-minute human-decision
   window the old design had to defend against, but the double-check
   stays because it's still cheap and still correct.

**Signal logic is the same tick-driven logic as the paper engine**
(Phase 7): `generate_builtin_signals`/`detect_todays_signal` for the
daily layer, `check_universal_stop_loss` for the intraday layer, and
`apply_fill`/`Position` for position accounting -- all imported directly
from `src.engine.backtest`/`src.engine.paper_trading`, never
reimplemented. Position accounting (`LivePosition`) advances only when the
broker *confirms* a fill (`reconcile_pending_trades`), at the average price the
broker reports, and records an estimate of what the fill cost. Sending an order
moves nothing: a rejected or cancelled order never leaves a position the
account does not hold. While an order is unconfirmed, no new order is decided
for that strategy and symbol, because the position those decisions read has
not moved yet.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pandas as pd
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.base import BrokerAdapter, OrderRequest, OrderSide, OrderType
from src.core.config import get_settings
from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.friction import compute_leg_costs
from src.engine.backtest.signals import BuiltinStrategy, generate_builtin_signals
from src.engine.paper_trading.daily_signal import detect_todays_signal
from src.engine.paper_trading.position_ledger import Position, apply_fill
from src.engine.paper_trading.price_data import InsufficientHistoryError, PriceDataProvider
from src.engine.paper_trading.quote_guard import exchange_stamp_problem
from src.engine.paper_trading.stop_loss import check_universal_stop_loss
from src.engine.risk.compliance import ExchangeCircuitLimits, RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.data_source import DataSource
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.order import Order
from src.models.strategy import Strategy, StrategyStatus
from src.models.trade import Trade
from src.observability.metrics import paper_quotes_refused_exchange_stale_total
from src.orchestration.engine_alerts import AlertCandidate, record_event_safely
from src.orchestration.kill_switch import assert_not_tripped
from src.orchestration.live_intent_expiry import expire_stale_intents
from src.orchestration.paper_trading import friction_model
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

logger = structlog.get_logger(__name__)

# Actor recorded on every audit entry this module writes for an
# autonomously-resolved intent -- there is no human approver to name
# anymore, so this says plainly what actually acted, matching this
# codebase's "never fabricate an actor" posture.
_AUTONOMOUS_ACTOR = "system:autonomous-safety-layer"

DAILY_LOOKBACK_DAYS = 90
DEFAULT_INTENT_EXPIRY_SECONDS = 90
# Build Spec §12.2's hard platform-wide maximum -- 5 minutes. Enforced
# both here (Python-level, at enrollment) and in the DB CheckConstraint
# on live_trading_subscriptions.intent_expiry_seconds.
MAX_INTENT_EXPIRY_SECONDS = 300

# Phase 18 Part 3's standing-cap defaults -- deliberately conservative,
# operator-editable per subscription (enroll_in_live_trading's kwargs, or
# any time after via a direct update).
DEFAULT_MAX_INTENTS_PER_WINDOW = 5
DEFAULT_RATE_LIMIT_WINDOW_MINUTES = 60
DEFAULT_MAX_NOTIONAL_PER_INTENT = 50_000.0

# Terminal statuses that represent a real, attempted order -- counted
# towards a subscription's own rolling-window rate cap. 'capped' is
# deliberately excluded: a cap rejection isn't an attempt, so it must
# never count towards exhausting its own limit.
_REAL_ATTEMPT_STATUSES = ("generated", "submitted", "failed", "expired")

_LIVE_ELIGIBLE_STATUSES = (StrategyStatus.LIVE_ELIGIBLE.value, StrategyStatus.LIVE.value)

__all__ = [
    "IntentExpiryWindowTooLongError",
    "NoLiveSubscriptionError",
    "StrategyNotLiveEligibleError",
    "SubscriptionNotFoundError",
    "enroll_in_live_trading",
    "expire_stale_intents",
    "generate_live_order_intent",
    "reconcile_pending_trades",
    "run_live_daily_signal_generation",
    "set_autonomous_trading",
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


class NoLiveSubscriptionError(Exception):
    def __init__(self, strategy_id: uuid.UUID, symbol: str):
        self.strategy_id = strategy_id
        self.symbol = symbol
        super().__init__(f"no live trading subscription for strategy {strategy_id} / {symbol}")


class SubscriptionNotFoundError(Exception):
    def __init__(self, subscription_id: uuid.UUID):
        self.subscription_id = subscription_id
        super().__init__(f"live trading subscription {subscription_id} not found")


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
    max_intents_per_window: int = DEFAULT_MAX_INTENTS_PER_WINDOW,
    rate_limit_window_minutes: int = DEFAULT_RATE_LIMIT_WINDOW_MINUTES,
    max_notional_per_intent: float = DEFAULT_MAX_NOTIONAL_PER_INTENT,
    created_by: str | None = None,
) -> LiveTradingSubscription:
    """Enrollment never accepts an initial autonomy value -- there is no
    `autonomous_trading_enabled` parameter here, deliberately, so a
    subscription can never be created already-autonomous even by
    accident. The only way to turn it on is `set_autonomous_trading`,
    below, as its own explicit, separately-audited act (Non-Negotiable
    Rule #1)."""
    if not (0 < intent_expiry_seconds <= MAX_INTENT_EXPIRY_SECONDS):
        raise IntentExpiryWindowTooLongError(intent_expiry_seconds)
    if max_intents_per_window <= 0:
        raise ValueError("max_intents_per_window must be positive")
    if rate_limit_window_minutes <= 0:
        raise ValueError("rate_limit_window_minutes must be positive")
    if max_notional_per_intent <= 0:
        raise ValueError("max_notional_per_intent must be positive")

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
        max_intents_per_window=max_intents_per_window,
        rate_limit_window_minutes=rate_limit_window_minutes,
        max_notional_per_intent=max_notional_per_intent,
        created_by=created_by,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def set_autonomous_trading(
    db: AsyncSession, subscription_id: uuid.UUID, *, enabled: bool, actor: str
) -> LiveTradingSubscription:
    """The master switch's only mutation path (Part 2, Non-Negotiable
    Rule #1). `actor` is required and never defaulted -- same posture as
    `src.orchestration.kill_switch.reset_kill_switch`'s `reset_by`, for
    the same reason: an automated or anonymous flip of this switch is not
    a valid call. Writes an audit-log row only on a genuine change (an
    idempotent call -- enabling an already-enabled subscription -- is a
    no-op, not a fresh audit entry), so the audit trail records real
    flips, not repeated confirmations."""
    if not actor:
        raise ValueError("set_autonomous_trading requires an explicit actor")

    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None:
        raise SubscriptionNotFoundError(subscription_id)

    if subscription.autonomous_trading_enabled == enabled:
        return subscription

    now = datetime.now(UTC)
    subscription.autonomous_trading_enabled = enabled
    subscription.autonomy_enabled_by = actor if enabled else None
    subscription.autonomy_enabled_at = now if enabled else None

    await write_audit_entry(
        db,
        actor=actor,
        action="live_trading_subscription.autonomy_enabled"
        if enabled
        else "live_trading_subscription.autonomy_disabled",
        entity_type="live_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "strategy_id": str(subscription.strategy_id),
            "symbol": subscription.symbol,
            "max_intents_per_window": subscription.max_intents_per_window,
            "rate_limit_window_minutes": subscription.rate_limit_window_minutes,
            "max_notional_per_intent": subscription.max_notional_per_intent,
        },
    )
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

        try:
            bars = price_provider.daily_bars(
                subscription.symbol, as_of=pd.Timestamp(as_of), lookback_days=DAILY_LOOKBACK_DAYS
            )
        except InsufficientHistoryError as exc:
            # A live signal must never come from generated bars: skip and say why.
            logger.warning(
                "live_trading.signal_skipped_awaiting_history",
                subscription_id=str(subscription.id),
                symbol=subscription.symbol,
                have=exc.have,
                need=exc.need,
            )
            continue
        # A real DB CHECK constraint (ck_live_trading_subscriptions_builtin_strategy)
        # already restricts this column to exactly these two literal values --
        # the Mapped column itself is plain `str` since SQLAlchemy has no way
        # to express that constraint in its own type.
        signals = generate_builtin_signals(
            bars, cast(BuiltinStrategy, subscription.builtin_strategy), subscription.sma_window
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
    db: AsyncSession, *, strategy_id: uuid.UUID, symbol: str, commit: bool = True
) -> LivePosition:
    """The strategy's position in `symbol`, created flat on first use. `commit=False` leaves the
    new row in the caller's transaction (the reconciliation batch commits once)."""
    result = await db.execute(
        select(LivePosition).where(
            LivePosition.strategy_id == strategy_id, LivePosition.symbol == symbol
        )
    )
    position = result.scalar_one_or_none()
    if position is None:
        position = LivePosition(strategy_id=strategy_id, symbol=symbol, quantity=0, avg_cost=0.0)
        db.add(position)
        if commit:
            await db.commit()
            await db.refresh(position)
        else:
            await db.flush()
    return position


async def _has_unconfirmed_order(db: AsyncSession, *, strategy_id: uuid.UUID, symbol: str) -> bool:
    """Whether an order for this strategy and symbol has been sent and the broker has not yet said
    how it ended. The position only moves on a confirmed fill, so deciding again before then would
    read a position that is about to change."""
    count = (
        await db.execute(
            select(func.count(Trade.id))
            .join(Order, Trade.order_id == Order.id)
            .where(
                Order.strategy_id == strategy_id,
                Order.symbol == symbol,
                Trade.status == "pending_confirmation",
            )
        )
    ).scalar_one()
    return count > 0


def _estimated_costs(side: str, quantity: int, price: float) -> float:
    """What this fill probably cost, with the paper engine's friction model (delivery or intraday
    as `PAPER_TRADING_PRODUCT` says). An estimate: the broker's contract note is not read."""
    from src.core.config import get_settings

    turnover = quantity * price
    legs = compute_leg_costs(
        turnover=turnover,
        is_buy=side == "buy",
        is_delivery=get_settings().paper_trading_product == "cnc",
        model=friction_model(),
    )
    return legs.total


async def _count_recent_real_attempts(
    db: AsyncSession, *, strategy_id: uuid.UUID, window_start: datetime
) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(LiveOrderIntent)
        .where(
            LiveOrderIntent.strategy_id == strategy_id,
            LiveOrderIntent.generated_at >= window_start,
            LiveOrderIntent.status.in_(_REAL_ATTEMPT_STATUSES),
        )
    )
    return result.scalar_one()


async def generate_live_order_intent(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    tick_price: float,
    adapter: BrokerAdapter | None = None,
    regulatory_provider: RegulatoryDataProvider | None = None,
) -> LiveOrderIntent | None:
    """Returns the `LiveOrderIntent` this tick produced, or `None` if the
    tick triggered no action (no signal, autonomy disabled, or the
    subscription/strategy isn't eligible). Raises `KillSwitchTrippedError`
    (uncaught) when the live kill switch is tripped -- callers (the
    intraday scheduler job) must catch this per-tick, the same posture as
    Phase 7's `process_tick`/tick-drain job, so one tripped switch doesn't
    crash the whole drain loop for every other subscription too. See this
    module's own docstring for the full, deliberate check order."""
    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None or not subscription.is_active:
        return None

    # The master autonomy switch -- checked before the Kill Switch,
    # deliberately (see module docstring). A subscription nobody has
    # opted in stays completely silent, full stop, no row at all.
    if not subscription.autonomous_trading_enabled:
        return None

    await assert_not_tripped(db, "live")

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

    # Standing rate/notional cap (Part 3) -- the last gate before an
    # intent is even created. Exceeding either bound produces a `capped`
    # row (logged, never silently dropped) instead of a real attempt.
    window_start = now - timedelta(minutes=subscription.rate_limit_window_minutes)
    recent_count = await _count_recent_real_attempts(
        db, strategy_id=strategy.id, window_start=window_start
    )
    over_rate_cap = recent_count >= subscription.max_intents_per_window
    over_notional_cap = notional > subscription.max_notional_per_intent

    if over_rate_cap or over_notional_cap:
        capped_intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol=subscription.symbol,
            side=side,
            quantity=quantity,
            intent_type=intent_type,
            expires_at=now + timedelta(seconds=subscription.intent_expiry_seconds),
            status="capped",
        )
        db.add(capped_intent)
        await db.flush()
        await write_audit_entry(
            db,
            actor=_AUTONOMOUS_ACTOR,
            action="live_order_intent.capped",
            entity_type="live_order_intent",
            entity_id=str(capped_intent.id),
            details={
                "reason": "rate_limit" if over_rate_cap else "notional_limit",
                "recent_count": recent_count,
                "max_intents_per_window": subscription.max_intents_per_window,
                "rate_limit_window_minutes": subscription.rate_limit_window_minutes,
                "notional": notional,
                "max_notional_per_intent": subscription.max_notional_per_intent,
            },
        )
        logger.warning(
            "live_trading.intent_capped",
            intent_id=str(capped_intent.id),
            strategy_id=str(strategy.id),
            reason="rate_limit" if over_rate_cap else "notional_limit",
            recent_count=recent_count,
            notional=notional,
        )
        await db.commit()
        await db.refresh(capped_intent)
        return capped_intent

    # An order already sent for this strategy and symbol has not been confirmed: the position above
    # is about to change, so no second decision is made on it.
    if await _has_unconfirmed_order(db, strategy_id=strategy.id, symbol=subscription.symbol):
        logger.info(
            "live_trading.decision_waits_for_unconfirmed_order",
            strategy_id=str(strategy.id),
            symbol=subscription.symbol,
        )
        return None

    intent = LiveOrderIntent(
        strategy_id=strategy.id,
        symbol=subscription.symbol,
        side=side,
        quantity=quantity,
        intent_type=intent_type,
        expires_at=now + timedelta(seconds=subscription.intent_expiry_seconds),
        status="generated",
    )
    db.add(intent)
    await db.commit()
    await db.refresh(intent)

    if adapter is None or regulatory_provider is None:
        # No broker configured right now -- left 'generated'; the expiry
        # sweep resolves it to 'expired' if nothing ever submits it. See
        # module docstring: this is the one rare case a `generated` row
        # outlives its own generation call.
        logger.warning(
            "live_trading.generated_intent_cannot_submit_no_adapter",
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
        actor=_AUTONOMOUS_ACTOR,
        action="live_order_intent.submission_failed",
        entity_type="live_order_intent",
        entity_id=str(intent.id),
        details={"reason": reason},
    )
    # A real order that was decided and not sent is something a person should hear about; a
    # stop-loss exit that could not be sent leaves a position unprotected, which is critical.
    # (This commits.)
    is_stop = intent.intent_type == "stop"
    await record_event_safely(
        db,
        AlertCandidate(
            key=f"live_intent_failed:{intent.id}",
            kind="live_intent_failed",
            severity="critical" if is_stop else "warning",
            title=(
                f"Live {intent.side} of {intent.symbol} was not sent"
                + (" (a stop-loss exit)" if is_stop else "")
            ),
            detail=reason[:2000],
        ),
    )
    await db.refresh(intent)
    return intent


async def _submit_intent_to_broker(
    db: AsyncSession,
    intent: LiveOrderIntent,
    *,
    adapter: BrokerAdapter,
    regulatory_provider: RegulatoryDataProvider,
) -> LiveOrderIntent:
    """The one function in this codebase that ever turns a `generated`
    intent into a real broker call -- since Phase 18, called exactly once,
    synchronously, from inside `generate_live_order_intent` itself, never
    from a separate human-triggered step. Re-quotes the symbol right
    before submission rather than trusting a possibly-stale
    generation-time price -- a real market order is priced at execution,
    not at planning time, and `live_order_intents` has no price column of
    its own (Build Spec §12.3's schema is exact). `create_order_intent`
    re-checks the Kill Switch here too (see module docstring): the only
    window left for a trip to matter is the few milliseconds between the
    generation-time check and this call, not a multi-minute human-decision
    window."""
    try:
        quote = await adapter.get_quote(intent.symbol)
    except Exception as exc:  # noqa: BLE001 - any quote failure is an honest submission failure
        return await _fail_intent(db, intent, reason=f"quote lookup failed: {exc}")

    # A quote the exchange stamped on an earlier day (or, if configured, too long ago) is not a
    # price to put a real order against.
    stale = exchange_stamp_problem(
        quote.exchange_time,
        now=datetime.now(UTC),
        max_age_seconds=get_settings().paper_quote_max_exchange_age_seconds,
    )
    if stale is not None:
        paper_quotes_refused_exchange_stale_total.labels(broker=adapter.broker_name).inc()
        return await _fail_intent(db, intent, reason=f"quote refused: {stale}")

    quote_price = quote.last_price
    subscription = await _get_live_subscription(
        db, strategy_id=intent.strategy_id, symbol=intent.symbol
    )

    # The circuit band is judged against the exchange's own limits from this quote or, without them,
    # against the previous close (the last daily bar's close, as of the morning's signal run). It
    # used to be judged against the quote's own price, which can never be outside its own band.
    exchange_limits = (
        ExchangeCircuitLimits(lower=quote.lower_circuit, upper=quote.upper_circuit)
        if quote.lower_circuit is not None and quote.upper_circuit is not None
        else None
    )
    stop_loss_exit = intent.intent_type == "stop"
    previous_close = subscription.last_signal_reference_price
    if exchange_limits is None and previous_close is None and not stop_loss_exit:
        return await _fail_intent(
            db,
            intent,
            reason=(
                "the circuit band cannot be judged: the quote carried no exchange circuit limits "
                "and there is no previous close to measure the reference table's band from"
            ),
        )

    try:
        await create_order_intent(
            db,
            mode="live",
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            proposed_price=quote_price,
            reference_price=previous_close if previous_close is not None else quote_price,
            proposed_position_value=intent.quantity * quote_price,
            portfolio_value=subscription.initial_capital,
            regulatory_provider=regulatory_provider,
            exchange_limits=exchange_limits,
            stop_loss_exit=stop_loss_exit,
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
            price=quote_price,
            status="pending_confirmation",
            data_source=DataSource.REAL,  # a broker's own quote, via a real adapter
        )
    )

    # The position is not touched here: it moves when the broker confirms the fill.

    intent.status = "submitted"
    intent.resulting_order_id = order.id

    await write_audit_entry(
        db,
        actor=_AUTONOMOUS_ACTOR,
        action="live_order_intent.submitted",
        entity_type="live_order_intent",
        entity_id=str(intent.id),
        details={
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "broker_name": adapter.broker_name,
            "broker_order_id": result.broker_order_id,
            "reference_price": quote_price,
            "circuit_band": (
                "skipped (stop-loss exit)"
                if stop_loss_exit
                else "exchange limits"
                if exchange_limits is not None
                else "previous close"
            ),
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


async def _apply_confirmed_fill(
    db: AsyncSession, trade: Trade, strategy_id: uuid.UUID | None
) -> None:
    """Move the strategy's position by a fill the broker has confirmed. The price is the broker's
    average price; when the broker reports a completed order without one, the price the order was
    placed against is used and the trade's `fill_price` stays empty to say so. The cost is an
    estimate (see `_estimated_costs`). Runs once per trade: only a `pending_confirmation` trade is
    ever reconciled."""
    if strategy_id is None:
        return
    price = trade.fill_price if trade.fill_price is not None else trade.price
    position = await _get_or_create_live_position(
        db, strategy_id=strategy_id, symbol=trade.symbol, commit=False
    )
    application = apply_fill(
        Position(quantity=position.quantity, avg_cost=position.avg_cost),
        side=trade.side,
        fill_quantity=trade.quantity,
        fill_price=price,
    )
    position.quantity = application.new_position.quantity
    position.avg_cost = application.new_position.avg_cost
    position.realized_pnl += application.realized_pnl
    costs = _estimated_costs(trade.side, trade.quantity, price)
    trade.costs_total = costs
    position.costs_total += costs


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
            select(Trade, Order.broker_order_id, Order.strategy_id)
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
    for trade, broker_order_id, strategy_id in pending:
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
        if outcome == "filled":
            if broker_order.average_price is not None:
                trade.fill_price = broker_order.average_price
            await _apply_confirmed_fill(db, trade, strategy_id)

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
