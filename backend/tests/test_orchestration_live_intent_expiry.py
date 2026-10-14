"""The live-intent expiry sweep, on its own.

`expire_stale_intents` resolves every *unsubmitted* intent past its `expires_at` to
`expired` -- with no order ever submitted -- and must be exactly right even when nobody
is watching. These tests insert intent rows directly (no broker, no signal pipeline),
so the mutation gate (`scripts/mutation_gate.py`) can run them quickly against just
`src/orchestration/live_intent_expiry.py`; the end-to-end behaviour is also covered in
`test_orchestration_live_trading.py`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.live_order_intent import LiveOrderIntent
from src.orchestration.live_intent_expiry import SWEPT_STATUSES, expire_stale_intents
from src.orchestration.strategies import create_strategy

SWEPT = ("pending_approval", "approved", "generated")
NOT_SWEPT = ("rejected", "expired", "submitted", "failed", "capped")


async def _strategy_id(db_session_factory):
    async with db_session_factory() as db:
        return (await create_strategy(db, name="ExpirySweep", objective="o")).id


async def _add(db_session_factory, strategy_id, status: str, expires_in: float):
    async with db_session_factory() as db:
        intent = LiveOrderIntent(
            strategy_id=strategy_id,
            symbol="DEMOSTOCK",
            side="buy",
            quantity=10,
            intent_type="entry",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            status=status,
        )
        db.add(intent)
        await db.commit()
        await db.refresh(intent)
        return intent.id


async def _status(db_session_factory, intent_id) -> str:
    async with db_session_factory() as db:
        return (await db.get(LiveOrderIntent, intent_id)).status


def test_the_swept_statuses_are_exactly_the_unsubmitted_ones():
    assert set(SWEPT_STATUSES) == set(SWEPT)


@pytest.mark.parametrize("status", SWEPT)
async def test_each_unsubmitted_status_past_its_window_expires(db_session_factory, status):
    strategy_id = await _strategy_id(db_session_factory)
    intent_id = await _add(db_session_factory, strategy_id, status, expires_in=-1)

    async with db_session_factory() as db:
        count = await expire_stale_intents(db)

    assert count == 1
    assert await _status(db_session_factory, intent_id) == "expired"


@pytest.mark.parametrize("status", NOT_SWEPT)
async def test_a_status_that_is_already_resolved_is_never_touched(db_session_factory, status):
    strategy_id = await _strategy_id(db_session_factory)
    intent_id = await _add(db_session_factory, strategy_id, status, expires_in=-3600)

    async with db_session_factory() as db:
        count = await expire_stale_intents(db)

    assert count == 0  # long past its window, but not the sweep's to resolve
    assert await _status(db_session_factory, intent_id) == status


@pytest.mark.parametrize("status", SWEPT)
async def test_an_intent_still_inside_its_window_is_left_alone(db_session_factory, status):
    strategy_id = await _strategy_id(db_session_factory)
    intent_id = await _add(db_session_factory, strategy_id, status, expires_in=300)

    async with db_session_factory() as db:
        count = await expire_stale_intents(db)

    assert count == 0
    assert await _status(db_session_factory, intent_id) == status


async def test_it_expires_exactly_the_stale_ones_and_counts_them(db_session_factory):
    strategy_id = await _strategy_id(db_session_factory)
    stale_a = await _add(db_session_factory, strategy_id, "generated", expires_in=-5)
    stale_b = await _add(db_session_factory, strategy_id, "pending_approval", expires_in=-60)
    fresh = await _add(db_session_factory, strategy_id, "generated", expires_in=120)
    submitted = await _add(db_session_factory, strategy_id, "submitted", expires_in=-5)

    async with db_session_factory() as db:
        count = await expire_stale_intents(db)

    assert count == 2
    assert await _status(db_session_factory, stale_a) == "expired"
    assert await _status(db_session_factory, stale_b) == "expired"
    assert await _status(db_session_factory, fresh) == "generated"
    assert await _status(db_session_factory, submitted) == "submitted"


async def test_a_second_sweep_finds_nothing_more_to_do(db_session_factory):
    strategy_id = await _strategy_id(db_session_factory)
    await _add(db_session_factory, strategy_id, "generated", expires_in=-5)

    async with db_session_factory() as db:
        first = await expire_stale_intents(db)
    async with db_session_factory() as db:
        second = await expire_stale_intents(db)

    assert (first, second) == (1, 0)


async def test_with_nothing_to_expire_it_reports_zero(db_session_factory):
    async with db_session_factory() as db:
        assert await expire_stale_intents(db) == 0


async def test_the_result_is_committed_not_just_flushed(db_session_factory):
    """The sweep runs on its own session from a scheduler: if it did not commit, the
    expiry would vanish when that session closed and the intent would stay live."""
    strategy_id = await _strategy_id(db_session_factory)
    intent_id = await _add(db_session_factory, strategy_id, "generated", expires_in=-5)

    async with db_session_factory() as db:
        await expire_stale_intents(db)

    async with db_session_factory() as other:  # a different session sees it
        rows = (await other.execute(select(LiveOrderIntent.status))).scalars().all()
    assert rows == ["expired"] and await _status(db_session_factory, intent_id) == "expired"
