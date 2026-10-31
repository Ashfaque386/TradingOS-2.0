"""E2E test-fixture seeding for the Go-Live evidence (e2e/go-live-evidence.spec.ts).

The evidence is worked out from fills, a backtest and Shadow Mode runs the system recorded. Reaching
thirty real-price closing fills over three weeks and ten clean trading days through the engine would
take a month, so this writes the records directly, the way an earlier run would have left them. A
strategy is then sitting in Paper Trading with its live-eligibility request already approved, ready
for the spec to sign off in the browser.

Two kinds:

- `passing`: 31 closing fills on real prices over 25 days (a little over half of them wins), a
  completed backtest on real data with a similar win rate, and a dry run that worked on each of the
  last ten NSE trading days. Everything the gate asks for.
- `weak`: a few fills on generated prices, one hand-made cleanup and a backtest on supplied bars, no
  dry runs. Nothing counts, and the evidence says why.

Usage:
    python scripts/seed_e2e_go_live.py passing|weak
"""

import asyncio
import sys
import uuid
from datetime import UTC, datetime, time, timedelta

from src.core.db import get_session_factory
from src.data.nse_calendar import is_nse_trading_day
from src.engine.paper_trading.market_hours import IST
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeRun
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.strategies import (
    LIVE_ELIGIBILITY_TRANSITION_TYPE,
    PROMOTION_TRANSITION_TYPE,
    promote_to_paper_trading,
    run_strategy_pipeline,
)


def _fill(sub_id, *, pnl, source="mixed", when, administrative=False):
    return PaperFill(
        subscription_id=sub_id,
        symbol="E2EGL",
        side="sell",
        order_group_id=uuid.uuid4(),
        requested_quantity=1,
        filled_quantity=1,
        avg_fill_price=100.0,
        fully_filled=True,
        realized_pnl=pnl,
        data_source=source,
        administrative=administrative,
        created_at=when,
    )


async def _decide_latest(session_factory, strategy_id, transition_type) -> None:
    from sqlalchemy import select

    from src.models.approval_request import ApprovalRequest

    async with session_factory() as db:
        request = (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.subject_id == str(strategy_id),
                    ApprovalRequest.transition_type == transition_type,
                )
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
    async with session_factory() as db:
        await decide_approval_request(db, request.id, approve=True, decided_by="e2e-seed-script")


async def seed(kind: str) -> None:
    session_factory = get_session_factory()
    name = f"E2E GoLive {kind} {uuid.uuid4().hex[:6]}"
    async with session_factory() as db:
        strategy, version = await run_strategy_pipeline(
            db,
            name=name,
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    strategy_id, version_id = strategy.id, version.id

    async with session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )
    await _decide_latest(session_factory, strategy_id, PROMOTION_TRANSITION_TYPE)
    async with session_factory() as db:
        await promote_to_paper_trading(db, strategy_id)
    async with session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        )
    await _decide_latest(session_factory, strategy_id, LIVE_ELIGIBILITY_TRANSITION_TYPE)

    now = datetime.now(UTC)
    async with session_factory() as db:
        subscription = PaperTradingSubscription(
            strategy_version_id=version_id,
            symbol="E2EGL",
            builtin_strategy="always_long",
            created_by="e2e-seed-script",
        )
        db.add(subscription)
        await db.flush()
        if kind == "passing":
            for i in range(31):
                db.add(
                    _fill(
                        subscription.id,
                        pnl=10.0 if i % 2 == 0 else -5.0,
                        when=now - timedelta(days=25) + timedelta(hours=i),
                    )
                )
            db.add(
                BacktestRun(
                    strategy_version_id=version_id,
                    symbol="E2EGL",
                    start_date=now.date() - timedelta(days=365),
                    end_date=now.date(),
                    status=BacktestStatus.COMPLETED,
                    data_source="real",
                    metrics={"win_rate": 0.5},
                )
            )
            day, recorded = now.astimezone(IST).date(), 0
            while recorded < 10:
                if is_nse_trading_day(day):
                    db.add(
                        ShadowModeRun(
                            broker_name="upstox",
                            has_sandbox=True,
                            confidence="real_sandbox_dry_run",
                            symbol="E2EGL",
                            side="buy",
                            quantity=1,
                            order_payload={},
                            strategy_id=strategy_id,
                            outcome="ok",
                            created_at=datetime.combine(day, time(4, 0), tzinfo=UTC),
                        )
                    )
                    recorded += 1
                day -= timedelta(days=1)
        else:
            for i in range(4):
                db.add(
                    _fill(
                        subscription.id, pnl=25.0, source="synthetic", when=now - timedelta(days=i)
                    )
                )
            db.add(_fill(subscription.id, pnl=99.0, administrative=True, when=now))
            db.add(
                BacktestRun(
                    strategy_version_id=version_id,
                    symbol="E2EGL",
                    start_date=now.date() - timedelta(days=365),
                    end_date=now.date(),
                    status=BacktestStatus.COMPLETED,
                    data_source="supplied",
                    metrics={"win_rate": 0.9},
                )
            )
        await db.commit()

    print(f"strategy_id={strategy_id}")
    print(f"name={name}")


if __name__ == "__main__":
    asyncio.run(seed(sys.argv[1] if len(sys.argv) > 1 else "passing"))
