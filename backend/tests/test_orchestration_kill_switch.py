"""Kill Switch persistence tests (Build Spec §8): latching, never
self-clearing, and the live/paper switches never share state -- the
acceptance-critical behavior of this whole phase.
"""

import pytest

from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.orchestration.kill_switch import (
    assert_not_tripped,
    check_drawdown,
    get_kill_switch_state,
    reset_kill_switch,
)


async def test_trips_on_a_real_drawdown_breach(db_session_factory):
    async with db_session_factory() as db:
        state = await check_drawdown(db, "live", current_equity=80_000, peak_equity=100_000)

    assert state.tripped is True
    assert "20" in state.trip_reason


async def test_never_self_clears_on_recovery(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "paper", current_equity=80_000, peak_equity=100_000)

    async with db_session_factory() as db:
        state = await check_drawdown(db, "paper", current_equity=200_000, peak_equity=200_000)

    assert state.tripped is True, "a full recovery must never clear a tripped switch"


async def test_assert_not_tripped_blocks_once_tripped(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        with pytest.raises(KillSwitchTrippedError):
            await assert_not_tripped(db, "live")


async def test_live_and_paper_never_share_state(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        paper_state = await get_kill_switch_state(db, "paper")

    assert paper_state.tripped is False


async def test_only_an_explicit_human_reset_clears_it(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        with pytest.raises(KillSwitchTrippedError):
            await assert_not_tripped(db, "live")

    async with db_session_factory() as db:
        state = await reset_kill_switch(db, "live", reset_by="risk-manager-1")

    assert state.tripped is False
    assert state.reset_by == "risk-manager-1"

    async with db_session_factory() as db:
        await assert_not_tripped(db, "live")  # no longer raises


async def test_reset_requires_an_explicit_actor(db_session_factory):
    async with db_session_factory() as db:
        with pytest.raises(ValueError):
            await reset_kill_switch(db, "live", reset_by="")
