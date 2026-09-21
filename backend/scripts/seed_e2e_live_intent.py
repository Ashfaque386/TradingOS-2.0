"""E2E test-fixture seeding: a live-eligible strategy, an enrolled live
subscription, and one generated live order intent (Build Spec §21-22
hardening pass).

Mirrors the exact sequence backend/tests/test_orchestration_live_trading.py's
own `_make_live_eligible_strategy` + `enroll_in_live_trading` +
`run_live_daily_signal_generation` + `generate_live_order_intent` helpers
use -- the same DEMOSTOCK/seed=1/2026-09-09 combination already proven (by
that test suite) to produce a real BUY signal, not a guess. This script
exists because the Playwright suite (../e2e/) needs to reach the "one live
order intent pending sign-off" state to exercise the Sign-off Queue's
approve/reject/expiry UI -- driving there via the UI itself would mean
re-testing the full strategy lifecycle (already covered by
e2e/strategy-lifecycle.spec.ts) as a prerequisite for every sign-off-queue
test run.

Usage:
    python scripts/seed_e2e_live_intent.py             # pending intent
    python scripts/seed_e2e_live_intent.py --expire     # already-expired intent
"""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pandas as pd

from src.core.db import get_session_factory
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.live_order_intent import LiveOrderIntent
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.live_trading import (
    enroll_in_live_trading,
    expire_stale_intents,
    generate_live_order_intent,
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
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def _decide_latest(session_factory, *, strategy_id: uuid.UUID, transition_type: str) -> None:
    from sqlalchemy import select

    from src.models.approval_request import ApprovalRequest

    async with session_factory() as db:
        result = await db.execute(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.subject_id == str(strategy_id),
                ApprovalRequest.transition_type == transition_type,
            )
            .order_by(ApprovalRequest.created_at.desc())
            .limit(1)
        )
        request = result.scalar_one()
    async with session_factory() as db:
        await decide_approval_request(
            db, request.id, approve=True, decided_by="e2e-seed-script"
        )


async def seed(*, expire: bool) -> None:
    session_factory = get_session_factory()

    async with session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db,
            name=f"E2E Live Intent {uuid.uuid4().hex[:8]}",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    strategy_id = strategy.id

    async with session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )
    await _decide_latest(session_factory, strategy_id=strategy_id, transition_type=PROMOTION_TRANSITION_TYPE)
    async with session_factory() as db:
        await promote_to_paper_trading(db, strategy_id)

    async with session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        )
    await _decide_latest(
        session_factory, strategy_id=strategy_id, transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE
    )
    async with session_factory() as db:
        await approve_strategy_for_live_eligibility(db, strategy_id, readiness_input=_PASSING_READINESS)

    async with session_factory() as db:
        subscription = await enroll_in_live_trading(
            db,
            strategy_id=strategy_id,
            symbol="DEMOSTOCK",
            broker_name="zerodha",
            intent_expiry_seconds=90,
        )
    async with session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)
    async with session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    if expire:
        async with session_factory() as db:
            row = await db.get(LiveOrderIntent, intent.id)
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        async with session_factory() as db:
            await expire_stale_intents(db)

    print(f"strategy_id={strategy_id}")
    print(f"intent_id={intent.id}")
    print(f"expired={expire}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expire", action="store_true", help="force the intent into the past and sweep it")
    args = parser.parse_args()
    asyncio.run(seed(expire=args.expire))
