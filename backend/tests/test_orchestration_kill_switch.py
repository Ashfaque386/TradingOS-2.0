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
    assert state.tripped_at is not None


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
    assert state.reset_at is not None

    async with db_session_factory() as db:
        await assert_not_tripped(db, "live")  # no longer raises


async def test_reset_requires_an_explicit_actor(db_session_factory):
    async with db_session_factory() as db:
        with pytest.raises(ValueError):
            await reset_kill_switch(db, "live", reset_by="")


# Build Spec §21-22 hardening pass: mutation testing (a real mutmut run,
# not hypothesized) found the gaps below -- every existing test above
# either triggers a clear breach or checks the persisted state fields it
# was written for, so none of them noticed a logic-inversion bug that only
# shows up on a NON-breaching drawdown, nor an untested `last_drawdown_pct`
# field, nor what the human-facing notify() alert actually says.


async def test_no_trip_when_drawdown_is_below_threshold(db_session_factory):
    """newly_tripped is `not state.tripped and evaluation.should_trip`, not
    `or`. A real mutmut run flipped it to `or`, under which a fresh
    (never-tripped) switch with `not state.tripped == True` would evaluate
    `True or evaluation.should_trip` as True regardless of the actual
    drawdown -- incorrectly tripping on every single call, breach or not.
    Every existing test above only ever exercises a real breach, so none
    of them caught this. This also pins `last_drawdown_pct` to the exact
    computed value -- a real mutmut run separately hardcoded it to `None`
    and nothing caught that either."""
    async with db_session_factory() as db:
        state = await check_drawdown(db, "live", current_equity=95_000, peak_equity=100_000)

    assert state.tripped is False
    assert state.trip_reason is None
    assert state.tripped_at is None
    assert state.last_drawdown_pct == 5.0


async def test_notify_body_carries_the_real_trip_reason_not_a_generic_fallback(
    db_session_factory, monkeypatch
):
    """notify()'s body is `state.trip_reason or 'drawdown threshold
    breached'` -- a real mutmut run flipped `or` to `and`, which (since
    trip_reason is always a freshly-set non-empty string on this code
    path) means the human-facing alert would ALWAYS show the generic
    fallback text instead of the actual computed percentages a risk
    manager needs to see, and nothing caught it because no existing test
    asserted on what notify() was actually called with."""
    calls: list[str] = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append(body)
        return []

    monkeypatch.setattr("src.orchestration.kill_switch.notify", fake_notify)

    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=80_000, peak_equity=100_000)

    assert len(calls) == 1
    assert "20" in calls[0]
    assert calls[0] != "drawdown threshold breached"
