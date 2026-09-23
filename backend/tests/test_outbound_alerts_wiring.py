"""Outbound alert wiring tests (Build Spec §18's explicit test
requirement: "outbound alerts actually fire for each of the listed event
types (kill-switch trip, sign-off item created, go-live gate pass)").
Each test monkeypatches the `notify` reference the real orchestration
module actually calls (not a shared mock at the dispatch layer) so a
genuine domain event -- not a direct call to the notifications package --
is what triggers the alert.
"""

import uuid

import pandas as pd
from sqlalchemy import select

from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.approval_request import ApprovalRequest
from src.notifications.types import AlertLevel
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.kill_switch import check_drawdown
from src.orchestration.live_trading import (
    enroll_in_live_trading,
    generate_live_order_intent,
    run_live_daily_signal_generation,
    set_autonomous_trading,
)
from src.orchestration.strategies import (
    LIVE_ELIGIBILITY_TRANSITION_TYPE,
    PROMOTION_TRANSITION_TYPE,
    GoLiveGateNotPassedError,
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
_FAILING_READINESS = GoLiveReadinessInput(
    num_trades=1,
    calendar_days_running=1,
    clean_shadow_mode_streak_days=0,
    live_win_rate=0.1,
    backtest_win_rate=0.9,
)


def _recorder():
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    return calls, fake_notify


# --- Kill switch trip ----------------------------------------------------


async def test_kill_switch_trip_fires_a_kill_switch_alert(db_session_factory, monkeypatch):
    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.kill_switch.notify", fake_notify)

    async with db_session_factory() as db:
        state = await check_drawdown(db, "live", current_equity=80_000, peak_equity=100_000)

    assert state.tripped is True
    assert len(calls) == 1
    assert calls[0][0] == AlertLevel.KILL_SWITCH
    assert "live" in calls[0][3]["mode"]


async def test_kill_switch_already_tripped_does_not_refire(db_session_factory, monkeypatch):
    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.kill_switch.notify", fake_notify)

    async with db_session_factory() as db:
        await check_drawdown(db, "paper", current_equity=80_000, peak_equity=100_000)
    async with db_session_factory() as db:
        await check_drawdown(db, "paper", current_equity=70_000, peak_equity=100_000)

    assert len(calls) == 1, "only the initial trip transition should fire an alert"


async def test_kill_switch_no_breach_does_not_fire(db_session_factory, monkeypatch):
    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.kill_switch.notify", fake_notify)

    async with db_session_factory() as db:
        state = await check_drawdown(db, "live", current_equity=98_000, peak_equity=100_000)

    assert state.tripped is False
    assert calls == []


# --- Sign-off item: strategy approval request -----------------------------


async def test_approval_request_creation_fires_a_sign_off_alert(db_session_factory, monkeypatch):
    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.approvals.notify", fake_notify)

    async with db_session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(uuid.uuid4()),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )

    assert len(calls) == 1
    assert calls[0][0] == AlertLevel.SIGN_OFF


# --- Go-Live Readiness Gate pass -------------------------------------------


async def _make_paper_trading_strategy(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy, _version = await run_strategy_pipeline(
            db, name="AlertDemo", objective="obj", sandbox_runtime=RestrictedProcessSandboxRuntime()
        )
    strategy_id = strategy.id

    async with db_session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=PROMOTION_TRANSITION_TYPE,
        )
    async with db_session_factory() as db:
        request = (
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.subject_id == str(strategy_id),
                        ApprovalRequest.transition_type == PROMOTION_TRANSITION_TYPE,
                    )
                )
            )
            .scalars()
            .one()
        )
        await decide_approval_request(db, request.id, approve=True, decided_by="risk-manager-1")

    async with db_session_factory() as db:
        await promote_to_paper_trading(db, strategy_id)

    async with db_session_factory() as db:
        await create_approval_request(
            db,
            subject_type="strategy",
            subject_id=str(strategy_id),
            transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        )
    async with db_session_factory() as db:
        request = (
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.subject_id == str(strategy_id),
                        ApprovalRequest.transition_type == LIVE_ELIGIBILITY_TRANSITION_TYPE,
                    )
                )
            )
            .scalars()
            .one()
        )
        await decide_approval_request(db, request.id, approve=True, decided_by="risk-manager-2")

    return strategy_id


async def test_go_live_gate_pass_fires_a_go_live_alert(db_session_factory, monkeypatch):
    strategy_id = await _make_paper_trading_strategy(db_session_factory)

    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.strategies.notify", fake_notify)

    async with db_session_factory() as db:
        promoted = await approve_strategy_for_live_eligibility(
            db, strategy_id, readiness_input=_PASSING_READINESS
        )

    assert promoted is True
    go_live_calls = [c for c in calls if c[0] == AlertLevel.GO_LIVE]
    assert len(go_live_calls) == 1
    assert str(strategy_id) in go_live_calls[0][3]["strategy_id"]


async def test_go_live_gate_failure_does_not_fire_an_alert(db_session_factory, monkeypatch):
    strategy_id = await _make_paper_trading_strategy(db_session_factory)

    calls, fake_notify = _recorder()
    monkeypatch.setattr("src.orchestration.strategies.notify", fake_notify)

    async with db_session_factory() as db:
        try:
            await approve_strategy_for_live_eligibility(
                db, strategy_id, readiness_input=_FAILING_READINESS
            )
        except GoLiveGateNotPassedError:
            pass
        else:
            raise AssertionError("expected GoLiveGateNotPassedError for a failing readiness input")

    assert calls == []


# --- Retired by Phase 18: a live order intent was a sign-off item ----------
#
# generate_live_order_intent used to fire an AlertLevel.SIGN_OFF alert the
# moment a pending_approval intent was created -- the notification that told
# a human "come approve this." Phase 18 removed the per-order human-approval
# gate entirely: an intent now resolves itself (submitted/failed/capped)
# inside the same call that created it, with nothing left for a human to be
# summoned to sign off on. src.orchestration.live_trading no longer imports
# `notify` at all -- the test below is a positive assertion of that removal,
# not a hole in coverage.

_PRICE_PROVIDER = FakeDailyPriceProvider(seed_by_symbol={"ALERTSTOCK": 1})
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def test_live_order_intent_generation_no_longer_fires_a_sign_off_alert(
    db_session_factory,
):
    strategy_id = await _make_paper_trading_strategy(db_session_factory)
    async with db_session_factory() as db:
        await approve_strategy_for_live_eligibility(
            db, strategy_id, readiness_input=_PASSING_READINESS
        )
    async with db_session_factory() as db:
        subscription = await enroll_in_live_trading(
            db, strategy_id=strategy_id, symbol="ALERTSTOCK", broker_name="zerodha"
        )
    async with db_session_factory() as db:
        await set_autonomous_trading(db, subscription.id, enabled=True, actor="risk-manager-1")
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF)

    # No broker adapter passed -- the intent stays 'generated', neither
    # submitted nor failed, but still: no notify() call of any kind, for
    # any reason, anywhere in this module anymore.
    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db, subscription_id=subscription.id, tick_price=106.0
        )

    assert intent is not None
    assert intent.status == "generated"
