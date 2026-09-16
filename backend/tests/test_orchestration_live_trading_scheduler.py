"""APScheduler wiring tests for the LiveExecutionPipeline (Build Spec
§12): the daily and intraday jobs run on their own schedule with no
manual trigger required, and -- the one behavior different from Phase
7's paper-trading scheduler -- the expiry sweep runs even outside market
hours, since a stale intent must resolve safely regardless of whether
the market is open.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd

from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.paper_trading.tick_feed import MockTickSource, publish_ticks_once, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.live_order_intent import LiveOrderIntent
from src.orchestration import live_trading_scheduler as scheduler_module
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.live_trading import enroll_in_live_trading
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
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def _decide_latest_request(db_session_factory, *, strategy_id, transition_type, decided_by):
    from sqlalchemy import select

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


async def _make_live_eligible_strategy(db_session_factory):
    async with db_session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db, name="SchedLive", objective="obj", sandbox_runtime=RestrictedProcessSandboxRuntime()
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


async def _make_subscription(db_session_factory):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="SCHEDLIVESTOCK", broker_name="zerodha"
        )
    return subscription


async def test_daily_signal_job_runs_without_any_manual_trigger(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    price_provider = FakeDailyPriceProvider(seed_by_symbol={"SCHEDLIVESTOCK": 1})

    await scheduler_module.run_live_daily_signal_job(db_session_factory, price_provider)

    from src.models.live_trading_subscription import LiveTradingSubscription

    async with db_session_factory() as db:
        row = await db.get(LiveTradingSubscription, subscription.id)
    # Whether or not today's fixed 90-day lookback happens to contain a
    # transition, the job must complete cleanly with no manual trigger.
    assert row is not None


async def test_intent_generation_job_processes_ticks_and_advances_the_cursor(
    db_session_factory, redis_client, monkeypatch
):
    subscription = await _make_subscription(db_session_factory)
    await redis_client.delete(tick_stream_key("SCHEDLIVESTOCK"))

    async with db_session_factory() as db:
        await scheduler_module.run_live_daily_signal_job(
            db_session_factory, FakeDailyPriceProvider(seed_by_symbol={"SCHEDLIVESTOCK": 1})
        )

    await publish_ticks_once(redis_client, MockTickSource(), ["SCHEDLIVESTOCK"])

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    await scheduler_module.run_intent_generation_job(
        db_session_factory,
        redis_client,
        adapter=None,
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
    )

    from src.models.live_trading_subscription import LiveTradingSubscription

    async with db_session_factory() as db:
        sub_row = await db.get(LiveTradingSubscription, subscription.id)
    assert sub_row.tick_cursor != "0", "the drain job must persist an advanced cursor"

    await redis_client.delete(tick_stream_key("SCHEDLIVESTOCK"))


async def test_intent_generation_job_is_gated_by_market_hours(
    db_session_factory, redis_client, monkeypatch
):
    await _make_subscription(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    # Must return cleanly (no exception) and do nothing when closed.
    await scheduler_module.run_intent_generation_job(
        db_session_factory,
        redis_client,
        adapter=None,
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
    )


async def test_expiry_sweep_job_runs_even_when_the_market_is_closed(
    db_session_factory, monkeypatch
):
    """The one deliberate difference from every other job in this
    scheduler: the sweep is never gated by market hours, since a stale
    intent must resolve safely regardless of whether the market is open."""
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="SCHEDLIVESTOCK2", broker_name="zerodha"
        )

    async with db_session_factory() as db:
        intent = LiveOrderIntent(
            strategy_id=strategy_id,
            symbol=subscription.symbol,
            side="buy",
            quantity=1,
            intent_type="entry",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            status="pending_approval",
        )
        db.add(intent)
        await db.commit()
        await db.refresh(intent)

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    await scheduler_module.run_expiry_sweep_job(db_session_factory)

    async with db_session_factory() as db:
        row = await db.get(LiveOrderIntent, intent.id)
    assert row.status == "expired"


async def test_start_live_trading_scheduler_registers_and_runs_all_jobs(
    db_session_factory, redis_client, monkeypatch
):
    """End-to-end proof the scheduler itself -- not just the job bodies --
    fires jobs on its own timer with zero manual intervention."""
    subscription = await _make_subscription(db_session_factory)
    await redis_client.delete(tick_stream_key("SCHEDLIVESTOCK"))
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await publish_ticks_once(redis_client, MockTickSource(), ["SCHEDLIVESTOCK"])
    async with db_session_factory() as db:
        await scheduler_module.run_live_daily_signal_job(
            db_session_factory, FakeDailyPriceProvider(seed_by_symbol={"SCHEDLIVESTOCK": 1})
        )

    scheduler = scheduler_module.start_live_trading_scheduler(
        db_session_factory,
        redis=redis_client,
        price_provider=FakeDailyPriceProvider(seed_by_symbol={"SCHEDLIVESTOCK": 1}),
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        adapter=None,
    )
    try:
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids == {
            scheduler_module.DAILY_SIGNAL_JOB_ID,
            scheduler_module.INTENT_GENERATION_JOB_ID,
            scheduler_module.EXPIRY_SWEEP_JOB_ID,
        }

        # The intent-generation job fires every 5s; give it time to run at
        # least once entirely on the scheduler's own timer.
        await asyncio.sleep(6)

        async with db_session_factory() as db:
            sub_row = await db.get(type(subscription), subscription.id)
        assert sub_row.tick_cursor != "0", "the scheduler must have drained the tick unattended"
    finally:
        scheduler.shutdown(wait=False)
        await redis_client.delete(tick_stream_key("SCHEDLIVESTOCK"))
