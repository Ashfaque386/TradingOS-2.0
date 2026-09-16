"""LiveExecutionPipeline orchestration tests (Build Spec §12): intent
generation never auto-submits, an approved intent reaches a real broker
adapter and produces audit-logged Order/Trade rows, a rejected intent
never reaches the broker, bounded batch pre-authorization enforces its
bounds server-side, and -- the acceptance-critical case -- a tripped kill
switch stops intent GENERATION entirely, not just approval.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import select

from src.brokers.base import BrokerCredentials
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.audit_log import AuditLog
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.trade import Trade
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.kill_switch import check_drawdown
from src.orchestration.live_trading import (
    IntentExpiredError,
    IntentNotPendingError,
    StrategyNotLiveEligibleError,
    approve_live_order_intent,
    create_batch_authorization,
    enroll_in_live_trading,
    expire_stale_intents,
    generate_live_order_intent,
    reject_live_order_intent,
    run_live_daily_signal_generation,
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


async def test_generate_intent_on_a_buy_signal_is_pending_approval_never_auto_submitted(
    db_session_factory,
):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    assert intent is not None
    assert intent.status == "pending_approval"
    assert intent.intent_type == "entry"
    assert intent.side == "buy"
    assert intent.quantity > 0

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert orders == [], "pending_approval must never produce an Order row"


async def test_intent_expires_safely_when_unactioned_past_its_window(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db,
            strategy_id=strategy_id,
            symbol="DEMOSTOCK",
            broker_name="zerodha",
            intent_expiry_seconds=90,
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )
    assert intent.status == "pending_approval"

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


async def test_approving_an_intent_reaches_the_broker_and_writes_order_and_trade_rows(
    db_session_factory,
):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    adapter = _mock_adapter(_quote_and_order_handler(106.5, order_id="LIVE_OID_1"))

    async with db_session_factory() as db:
        approved = await approve_live_order_intent(
            db,
            intent.id,
            approved_by="risk-manager-1",
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert approved.status == "submitted"
    assert approved.resulting_order_id is not None

    async with db_session_factory() as db:
        order = await db.get(Order, approved.resulting_order_id)
        assert order.status == "submitted"
        assert order.broker_order_id == "LIVE_OID_1"

        trades = (await db.execute(select(Trade).where(Trade.order_id == order.id))).scalars().all()
        assert len(trades) == 1
        assert trades[0].price == 106.5
        assert trades[0].status == "pending_confirmation"

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

        position = (
            (await db.execute(select(LivePosition).where(LivePosition.strategy_id == strategy_id)))
            .scalars()
            .one()
        )
        assert position.quantity == intent.quantity
        assert position.avg_cost == 106.5


async def test_rejecting_an_intent_never_reaches_the_broker(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    async with db_session_factory() as db:
        rejected = await reject_live_order_intent(db, intent.id, rejected_by="risk-manager-1")

    assert rejected.status == "rejected"
    assert rejected.resulting_order_id is None

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert orders == [], "a rejected intent must never produce an order"

    # Re-approving a rejected intent must be refused, not silently retried.
    async with db_session_factory() as db:
        with pytest.raises(IntentNotPendingError):
            await approve_live_order_intent(
                db,
                intent.id,
                approved_by="risk-manager-1",
                adapter=_mock_adapter(lambda r: httpx.Response(500)),
                regulatory_provider=_REGULATORY_PROVIDER,
            )


async def test_approving_an_already_expired_intent_is_refused(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with db_session_factory() as db:
        with pytest.raises(IntentExpiredError):
            await approve_live_order_intent(
                db,
                intent.id,
                approved_by="risk-manager-1",
                adapter=_mock_adapter(lambda r: httpx.Response(500)),
                regulatory_provider=_REGULATORY_PROVIDER,
            )

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
    assert row.status == "expired"


async def test_batch_authorization_auto_approves_and_submits_within_bounds(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    now = datetime.now(UTC)
    async with db_session_factory() as db:
        batch = await create_batch_authorization(
            db,
            strategy_id=strategy_id,
            authorized_by="risk-manager-1",
            max_intents=3,
            max_notional_per_intent=1_000_000.0,
            window_start=now - timedelta(minutes=1),
            window_end=now + timedelta(minutes=30),
        )

    adapter = _mock_adapter(_quote_and_order_handler(106.5, order_id="BATCH_OID"))

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent.status == "submitted"
    assert intent.batch_authorization_id == batch.id
    assert intent.approved_by == "batch:risk-manager-1"

    async with db_session_factory() as db:
        refreshed_batch = await db.get(type(batch), batch.id)
    assert refreshed_batch.intents_used == 1


async def test_batch_authorization_never_lets_a_client_exceed_its_count_bound(db_session_factory):
    """The acceptance-critical server-side enforcement: an authorization
    already at its intent count cap must never auto-approve one more,
    even though the batch row itself still exists and could be named."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    now = datetime.now(UTC)
    async with db_session_factory() as db:
        batch = await create_batch_authorization(
            db,
            strategy_id=strategy_id,
            authorized_by="risk-manager-1",
            max_intents=1,
            max_notional_per_intent=1_000_000.0,
            window_start=now - timedelta(minutes=1),
            window_end=now + timedelta(minutes=30),
        )
        # Simulate the cap already being exhausted by a prior tick.
        batch.intents_used = 1
        await db.commit()

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=_mock_adapter(lambda r: httpx.Response(500)),
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent.status == "pending_approval", "an exhausted batch must never auto-approve"
    assert intent.batch_authorization_id is None


async def test_batch_authorization_never_lets_a_client_exceed_its_notional_cap(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    now = datetime.now(UTC)
    async with db_session_factory() as db:
        await create_batch_authorization(
            db,
            strategy_id=strategy_id,
            authorized_by="risk-manager-1",
            max_intents=5,
            # Deliberately far below what a real entry's notional would be
            # (initial_capital * position_size_pct/100 ~= a few thousand),
            # so this specific intent can never qualify no matter what a
            # client might claim.
            max_notional_per_intent=1.0,
            window_start=now - timedelta(minutes=1),
            window_end=now + timedelta(minutes=30),
        )

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=_mock_adapter(lambda r: httpx.Response(500)),
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert intent.status == "pending_approval", "notional over the cap must never auto-approve"
    assert intent.batch_authorization_id is None


async def test_a_tripped_kill_switch_stops_intent_generation_not_just_approval(db_session_factory):
    """The acceptance-critical Build Spec §12.2 behavior: a tripped kill
    switch must block intent GENERATION, not merely approval/submission --
    no LiveOrderIntent row is created at all, for a tick that would
    otherwise clearly have produced one."""
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
    assert intents == [], "a tripped kill switch must block intent creation, not just approval"


async def test_kill_switch_tripped_between_generation_and_approval_still_blocks_submission(
    db_session_factory,
):
    """Generation-time blocking is additive, not a replacement for the
    existing submission-time check: a switch tripped after a pending
    intent already exists must still block it reaching the broker."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="DEMOSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )
    assert intent.status == "pending_approval"

    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        approved = await approve_live_order_intent(
            db,
            intent.id,
            approved_by="risk-manager-1",
            adapter=_mock_adapter(lambda r: httpx.Response(500)),
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert approved.status == "failed"

    async with db_session_factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
    assert orders == [], "a kill switch tripped before submission must still block it"
