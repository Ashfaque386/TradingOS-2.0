"""E2E test-fixture seeding: a live-eligible strategy, ready to be
enrolled into live trading (Build Spec §21-22 hardening pass, Phase 18
redesign).

Mirrors the exact sequence backend/tests/test_orchestration_live_trading.py's
own `_make_live_eligible_strategy` helper uses. This script exists because
the Playwright suite (../e2e/) needs a fresh LiveEligible strategy for each
run of e2e/live-autonomy.spec.ts, and reaching that state through the UI
alone would mean re-testing the full strategy lifecycle (already covered by
e2e/strategy-lifecycle.spec.ts) as a prerequisite for every live-autonomy
spec run. Everything downstream of "LiveEligible strategy exists" --
enrolling a live-trading subscription, flipping its autonomy switch,
generating an intent from a simulated tick -- is deliberately left to the
spec itself to drive through the real UI, since that is exactly the new
surface this phase added and the part worth exercising end to end.

Usage:
    python scripts/seed_e2e_live_intent.py
"""

import asyncio
import uuid

from src.core.db import get_session_factory
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.orchestration.approvals import create_approval_request, decide_approval_request
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
# "DEMOSTOCK" is FakeDailyPriceProvider's default-seeded series (Phase 7
# verified this is stable across processes -- crc32-based, not Python's
# salted hash()); 2026-09-09 is a genuine flat->long SMA-crossover BUY day
# for it, which the spec uses via POST /live-trading/daily-signal-run.
SYMBOL = "DEMOSTOCK"


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
        await decide_approval_request(db, request.id, approve=True, decided_by="e2e-seed-script")


async def seed() -> None:
    session_factory = get_session_factory()

    strategy_name = f"E2E Live Autonomy {uuid.uuid4().hex[:8]}"
    async with session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db,
            name=strategy_name,
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
    await _decide_latest(
        session_factory, strategy_id=strategy_id, transition_type=PROMOTION_TRANSITION_TYPE
    )
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
        await approve_strategy_for_live_eligibility(
            db, strategy_id, readiness_input=_PASSING_READINESS
        )

    print(f"strategy_id={strategy_id}")
    print(f"symbol={SYMBOL}")
    print(f"name={strategy_name}")


if __name__ == "__main__":
    asyncio.run(seed())
