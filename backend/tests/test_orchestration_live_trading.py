"""LiveExecutionPipeline orchestration tests (Build Spec §12, redesigned
by Phase 18): autonomy is disabled by default and produces no intent at
all until explicitly enabled; the master switch is checked before the
Kill Switch; a tripped Kill Switch still blocks generation (and, in the
same call, submission) when autonomy is on; the standing rate/notional
cap stops a runaway signal loop independent of the Kill Switch; and the
expiry sweep still resolves the one rare "no broker adapter configured"
case, plus historical pre-Phase-18 rows, safely.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import select

from src.brokers.base import BrokerCredentials, BrokerQuote, OrderRequest
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.audit_log import AuditLog
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.order import Order
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.kill_switch import check_drawdown
from src.orchestration.live_trading import (
    IntentExpiryWindowTooLongError,
    StrategyNotLiveEligibleError,
    enroll_in_live_trading,
    expire_stale_intents,
    generate_live_order_intent,
    run_live_daily_signal_generation,
    set_autonomous_trading,
)
from src.orchestration.strategies import (
    LIVE_ELIGIBILITY_TRANSITION_TYPE,
    PROMOTION_TRANSITION_TYPE,
    approve_strategy_for_live_eligibility,
    promote_to_paper_trading,
    run_strategy_pipeline,
)

_PASSING_READINESS = GoLiveReadinessInput(
    num_trades=50,
    calendar_days_running=30,
    clean_shadow_mode_streak_days=15,
    live_win_rate=0.55,
    backtest_win_rate=0.5,
)

_PRICE_PROVIDER = FakeDailyPriceProvider(seed_by_symbol={"DEMOSTOCK": 1})
_REGULATORY_PROVIDER = ReferenceTableRegulatoryDataProvider()
# Known from this fake provider's fixed seeded series (Phase 7 verified
# this): 2026-09-09 is a genuine flat->long SMA-crossover BUY day.
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def _decide_latest_request(
    db_session_factory, *, strategy_id: uuid.UUID, transition_type: str, decided_by: str
) -> None:
    from src.models.approval_request import ApprovalRequest

    async with db_session_factory() as db:
        request = (
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.subject_id == str(strategy_id),
                        ApprovalRequest.transition_type == transition_type,
                    )
                )
            )
            .scalars()
            .one()
        )
        await decide_approval_request(db, request.id, approve=True, decided_by=decided_by)


async def _make_live_eligible_strategy(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db, name="LiveDemo", objective="obj", sandbox_runtime=RestrictedProcessSandboxRuntime()
        )
    strategy_id = strategy.id

    async with db_session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )
    await _decide_latest_request(
        db_session_factory,
        strategy_id=strategy_id,
        transition_type=PROMOTION_TRANSITION_TYPE,
        decided_by="risk-manager-1",
    )
    async with db_session_factory() as db:
        await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        )
    await _decide_latest_request(
        db_session_factory,
        strategy_id=strategy_id,
        transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        decided_by="risk-manager-2",
    )
    async with db_session_factory() as db:
        await approve_strategy_for_live_eligibility(
            db, strategy_id, readiness_input=_PASSING_READINESS
        )

    return strategy_id


async def _enroll_and_enable_autonomy(
    db_session_factory, strategy_id: uuid.UUID, **enroll_kwargs
) -> LiveTradingSubscription:
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha", **enroll_kwargs
        )
    async with db_session_factory() as db:
        subscription = await set_autonomous_trading(
            db, subscription.id, enabled=True, actor="risk-manager-1"
        )
    return subscription


def _mock_adapter(handler) -> ZerodhaKiteAdapter:
    return ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )


def _quote_and_order_handler(price: float, order_id: str = "OID1"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/quote":
            return httpx.Response(
                200, json={"data": {"DEMOSTOCK": {"last_price": price, "depth": {}}}}
            )
        return httpx.Response(200, json={"data": {"order_id": order_id}})

    return handler


async def test_enroll_requires_live_eligible_strategy(db_session_factory):
    async with db_session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db,
            name="NotEligible",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )

    async with db_session_factory() as db:
        with pytest.raises(StrategyNotLiveEligibleError):
            await enroll_in_live_trading(
                db, strategy_id=strategy.id, symbol="DEMOSTOCK", broker_name="zerodha"
            )


async def test_enroll_never_accepts_an_initial_autonomy_value(db_session_factory):
    """There is no `autonomous_trading_enabled` kwarg on
    `enroll_in_live_trading` at all -- a fresh subscription is always
    `False`, including for an already-LiveEligible strategy, per
    Non-Negotiable Rule #1."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    assert subscription.autonomous_trading_enabled is False
    assert subscription.autonomy_enabled_by is None
    assert subscription.autonomy_enabled_at is None


async def test_daily_signal_generation_updates_subscription_in_place(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )

    async with db_session_factory() as db:
        updated = await run_live_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )

    assert len(updated) == 1
    assert updated[0].id == subscription.id
    assert updated[0].last_signal_type == "BUY"
    assert updated[0].last_signal_reference_price > 0


async def test_generate_intent_with_autonomy_disabled_produces_no_intent_at_all(
    db_session_factory,
):
    """Non-Negotiable Rule #1's "full stop": a real BUY signal exists,
    but autonomy was never enabled -- generate_live_order_intent must
    return None and write no LiveOrderIntent row, not even a rejected
    one."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    assert subscription.autonomous_trading_enabled is False

    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    assert intent is None
    async with db_session_factory() as db:
        intents = (
            (
                await db.execute(
                    select(LiveOrderIntent).where(LiveOrderIntent.strategy_id == strategy_id)
                )
            )
            .scalars()
            .all()
        )
    assert intents == []


async def test_generate_intent_with_autonomy_enabled_auto_submits_and_reaches_broker(
    db_session_factory,
):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    adapter = _mock_adapter(_quote_and_order_handler(106.5, order_id="AUTO_OID_1"))

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent is not None
    assert intent.status == "submitted"
    assert intent.intent_type == "entry"
    assert intent.side == "buy"
    assert intent.resulting_order_id is not None
    # No human ever approved this -- both fields stay honestly empty.
    assert intent.approved_by is None
    assert intent.approved_at is None

    async with db_session_factory() as db:
        order = await db.get(Order, intent.resulting_order_id)
        assert order.status == "submitted"
        assert order.broker_order_id == "AUTO_OID_1"

        audit_entries = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "live_order_intent.submitted")
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_entries) == 1
        assert audit_entries[0].actor == "system:autonomous-safety-layer"

        position = (
            (await db.execute(select(LivePosition).where(LivePosition.strategy_id == strategy_id)))
            .scalars()
            .one()
        )
        assert position.quantity == intent.quantity
        assert position.avg_cost == 106.5


async def test_master_switch_checked_before_kill_switch(db_session_factory):
    """Autonomy left disabled + a tripped Kill Switch: generation must
    return `None` cleanly, NOT raise `KillSwitchTrippedError` -- proving
    the master-switch-off check short-circuits before the Kill Switch is
    ever consulted, per this module's own documented check order."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )
    assert intent is None


async def test_kill_switch_tripped_blocks_generation_when_autonomy_enabled(db_session_factory):
    """The acceptance-critical behavior carried over from the original
    design: with autonomy ON, a tripped Kill Switch must block intent
    GENERATION itself -- no `LiveOrderIntent` row created at all."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        with pytest.raises(KillSwitchTrippedError):
            await generate_live_order_intent(db, subscription_id=subscription.id, tick_price=106.0)

    async with db_session_factory() as db:
        intents = (
            (
                await db.execute(
                    select(LiveOrderIntent).where(LiveOrderIntent.strategy_id == strategy_id)
                )
            )
            .scalars()
            .all()
        )
    assert intents == [], "a tripped kill switch must block intent creation, not just submission"


async def test_kill_switch_tripped_mid_flight_still_blocks_submission(db_session_factory):
    """Part 1.4's required case: with no multi-minute human-decision
    window left for a trip to happen during, the only window that still
    matters is the few milliseconds inside one `generate_live_order_intent`
    call. Simulated here via a broker double whose `get_quote` trips the
    switch as a side effect right before returning -- the Kill Switch was
    off when generation started, but `_submit_intent_to_broker`'s own
    re-check (via `create_order_intent`) must still catch it and fail the
    intent, never place the order."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    class _TripKillSwitchMidFlightAdapter:
        broker_name = "zerodha"

        def __init__(self, session_factory):
            self._session_factory = session_factory

        async def get_quote(self, symbol: str) -> BrokerQuote:
            async with self._session_factory() as db:
                await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)
            return BrokerQuote(
                symbol=symbol, last_price=106.5, bid=None, ask=None, timestamp=datetime.now(UTC)
            )

        async def place_order(self, order: OrderRequest):
            raise AssertionError(
                "the kill switch tripped mid-flight -- place_order must never be called"
            )

    adapter = _TripKillSwitchMidFlightAdapter(db_session_factory)

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent.status == "failed"
    assert intent.resulting_order_id is None

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert orders == [], "a kill switch tripped mid-flight must still block the order"


async def test_standing_cap_blocks_a_runaway_signal_loop(db_session_factory):
    """Part 3.3's required case: a strategy misconfigured to fire an
    entry signal on every tick regardless of its own position (simulated
    here by forcing the position back to flat before each call, so a
    real BUY candidate is produced every single time -- exactly the
    "fires on every tick" misconfiguration) must stop generating real
    orders once it hits `max_intents_per_window`, independent of and
    well before the Kill Switch would ever matter."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(
        db_session_factory, strategy_id, max_intents_per_window=3, rate_limit_window_minutes=60
    )
    async with db_session_factory() as db:
        sub_row = await db.get(LiveTradingSubscription, subscription.id)
        sub_row.last_signal_type = "BUY"
        await db.commit()

    adapter = _mock_adapter(_quote_and_order_handler(106.5, order_id="RUNAWAY_OID"))

    real_statuses: list[str] = []
    for _ in range(8):
        async with db_session_factory() as db:
            position = (
                (
                    await db.execute(
                        select(LivePosition).where(LivePosition.strategy_id == strategy_id)
                    )
                )
                .scalars()
                .one_or_none()
            )
            if position is not None:
                position.quantity = 0
                position.avg_cost = 0.0
                await db.commit()

        async with db_session_factory() as db:
            intent = await generate_live_order_intent(
                db,
                subscription_id=subscription.id,
                tick_price=106.0,
                adapter=adapter,
                regulatory_provider=_REGULATORY_PROVIDER,
            )
        assert intent is not None
        real_statuses.append(intent.status)

    submitted = [s for s in real_statuses if s == "submitted"]
    capped = [s for s in real_statuses if s == "capped"]
    assert (
        len(submitted) == 3
    ), f"expected exactly the cap's worth of real orders, got {real_statuses}"
    assert len(capped) == 5

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert len(orders) == 3, "the broker must never see more orders than the standing cap allows"

    async with db_session_factory() as db:
        capped_audit = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "live_order_intent.capped")
                )
            )
            .scalars()
            .all()
        )
    assert len(capped_audit) == 5, "every cap rejection must be audited, never silently dropped"


async def test_standing_notional_cap_blocks_an_oversized_intent(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(
        db_session_factory,
        strategy_id,
        # Deliberately far below what a real entry's notional would be
        # (initial_capital * position_size_pct/100 ~= a few thousand), so
        # this specific intent can never qualify no matter the tick price.
        max_notional_per_intent=1.0,
    )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    def _explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the broker must never be called for a notional-capped intent")

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=_mock_adapter(_explode),
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent.status == "capped"
    assert intent.resulting_order_id is None


async def test_intent_expires_safely_when_unactioned_past_its_window(db_session_factory):
    """The one rare case a `generated` row outlives its own generation
    call: no broker adapter configured at generation time."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(
        db_session_factory, strategy_id, intent_expiry_seconds=90
    )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )
    assert intent.status == "generated"

    # Force it into the past rather than sleeping 90s in a test.
    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with db_session_factory() as db:
        expired_count = await expire_stale_intents(db)
    assert expired_count == 1

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
    assert row.status == "expired"

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert orders == [], "an expired intent must never have produced an order"


async def test_expires_a_historical_pre_phase18_pending_approval_row_too(db_session_factory):
    """The sweep's status filter still includes 'pending_approval'/
    'approved' -- not because new code ever writes them, but so a
    historical row left over from before Phase 18 still gets swept
    correctly rather than stuck forever."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )

    async with db_session_factory() as db:
        historical = LiveOrderIntent(
            strategy_id=strategy_id,
            symbol=subscription.symbol,
            side="buy",
            quantity=10,
            intent_type="entry",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            status="pending_approval",
        )
        db.add(historical)
        await db.commit()
        await db.refresh(historical)

    async with db_session_factory() as db:
        expired_count = await expire_stale_intents(db)
    assert expired_count == 1

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, historical.id)
    assert row.status == "expired"


async def test_does_not_expire_intents_still_within_their_window(db_session_factory):
    """The comparison is `expires_at <= now`, not something that fires
    early -- an intent well inside its window must be left completely
    alone by the sweep."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(
        db_session_factory, strategy_id, intent_expiry_seconds=300
    )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )
    assert intent.status == "generated"

    async with db_session_factory() as db:
        expired_count = await expire_stale_intents(db)
    assert expired_count == 0

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
    assert row.status == "generated"


async def test_does_not_touch_intents_already_in_a_terminal_state(db_session_factory):
    """The sweep only ever targets unsubmitted statuses -- a `submitted`
    intent whose `expires_at` has long passed must never be swept or
    counted, since it's already resolved and the sweep isn't the thing
    that resolved it."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=_mock_adapter(_quote_and_order_handler(106.5)),
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    assert intent.status == "submitted"

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with db_session_factory() as db:
        expired_count = await expire_stale_intents(db)
    assert expired_count == 0

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
    assert row.status == "submitted"


async def test_set_autonomous_trading_requires_an_explicit_actor(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )

    async with db_session_factory() as db:
        with pytest.raises(ValueError):
            await set_autonomous_trading(db, subscription.id, enabled=True, actor="")


async def test_set_autonomous_trading_writes_an_audit_entry_only_on_a_real_change(
    db_session_factory,
):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )

    async with db_session_factory() as db:
        enabled = await set_autonomous_trading(
            db, subscription.id, enabled=True, actor="risk-manager-1"
        )
    assert enabled.autonomous_trading_enabled is True
    assert enabled.autonomy_enabled_by == "risk-manager-1"
    assert enabled.autonomy_enabled_at is not None

    # Idempotent re-enable: no new audit entry, no state change.
    async with db_session_factory() as db:
        await set_autonomous_trading(db, subscription.id, enabled=True, actor="risk-manager-2")

    async with db_session_factory() as db:
        entries = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "live_trading_subscription.autonomy_enabled"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(entries) == 1

    async with db_session_factory() as db:
        disabled = await set_autonomous_trading(
            db, subscription.id, enabled=False, actor="risk-manager-1"
        )
    assert disabled.autonomous_trading_enabled is False
    assert disabled.autonomy_enabled_by is None
    assert disabled.autonomy_enabled_at is None


async def test_enroll_rejects_a_non_positive_standing_cap(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        with pytest.raises(ValueError):
            await enroll_in_live_trading(
                db,
                strategy_id=strategy_id,
                symbol="DEMOSTOCK",
                broker_name="zerodha",
                max_intents_per_window=0,
            )


async def test_intent_expiry_window_too_long_is_rejected_at_enrollment(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        with pytest.raises(IntentExpiryWindowTooLongError):
            await enroll_in_live_trading(
                db,
                strategy_id=strategy_id,
                symbol="DEMOSTOCK",
                broker_name="zerodha",
                intent_expiry_seconds=301,
            )
