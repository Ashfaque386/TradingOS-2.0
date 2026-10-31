"""The automatic kill switch (Phase 3): a measured reading is stored, judged against the applied
dual-control limit and, when the drawdown reaches it, trips the switch -- once, latched, with an
audit row. A reading that could not be measured is neither a loss nor a recovery.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.audit_log import AuditLog
from src.models.equity_snapshot import EquitySnapshot
from src.notifications.types import AlertLevel
from src.orchestration import drawdown_monitor
from src.orchestration.drawdown_monitor import MONITOR_ACTOR, evaluate_mode_drawdown
from src.orchestration.equity import EquitySample
from src.orchestration.kill_switch import get_kill_switch_state, reset_kill_switch
from src.orchestration.risk_limits import (
    apply_risk_limit_change,
    confirm_risk_limit_change,
    stage_risk_limit_change,
)

T0 = datetime(2026, 10, 30, 5, 0, tzinfo=UTC)


def sample(equity, mode="paper", *, note=None, source="real"):
    return EquitySample(
        mode=mode,
        equity=equity,
        capital_base=100_000.0,
        realized_net=0.0 if equity is not None else None,
        unrealized=0.0 if equity is not None else None,
        open_positions=0,
        unpriced_positions=0 if equity is not None else 1,
        price_source=source,
        note=note,
    )


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def fake_notify(level, *, title, body, details=None):
        sent.append((level, title, body, details))

    monkeypatch.setattr(drawdown_monitor, "notify", fake_notify)
    return sent


async def _snapshots(db, mode="paper"):
    rows = await db.execute(
        select(EquitySnapshot).where(EquitySnapshot.mode == mode).order_by(EquitySnapshot.taken_at)
    )
    return list(rows.scalars().all())


async def _apply_limit(db_session_factory, value):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="max_drawdown_pct", proposed_value=value, staged_by="a"
        )
        await confirm_risk_limit_change(db, change.id, confirmed_by="b")
        await apply_risk_limit_change(db, change.id, applied_by="b")


async def test_a_reading_below_the_limit_is_stored_and_trips_nothing(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        outcome = await evaluate_mode_drawdown(db, sample(95_000), now=T0 + timedelta(minutes=1))

    assert outcome.status == "ok"
    assert outcome.peak_equity == 100_000
    assert outcome.drawdown_pct == pytest.approx(5.0)
    assert outcome.threshold_pct == 15.0
    async with db_session_factory() as db:
        state = await get_kill_switch_state(db, "paper")
        rows = await _snapshots(db)
    assert state.tripped is False
    assert state.updated_at == T0 + timedelta(minutes=1)
    assert state.last_drawdown_pct == pytest.approx(5.0)
    assert [r.equity for r in rows] == [100_000, 95_000]
    assert rows[1].peak_equity == 100_000
    assert rows[1].drawdown_pct == pytest.approx(5.0)
    assert alerts == []


async def test_a_new_high_becomes_the_peak(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        await evaluate_mode_drawdown(db, sample(120_000), now=T0 + timedelta(minutes=1))
        outcome = await evaluate_mode_drawdown(db, sample(110_000), now=T0 + timedelta(minutes=2))

    assert outcome.peak_equity == 120_000
    assert outcome.drawdown_pct == pytest.approx(100 * 10_000 / 120_000)


async def test_the_first_reading_is_its_own_peak(db_session_factory, alerts):
    async with db_session_factory() as db:
        outcome = await evaluate_mode_drawdown(db, sample(80_000), now=T0)

    assert outcome.status == "ok"
    assert outcome.peak_equity == 80_000
    assert outcome.drawdown_pct == 0.0


async def test_reaching_the_limit_trips_the_switch_with_the_figures_an_audit_row_and_an_alert(
    db_session_factory, alerts
):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        outcome = await evaluate_mode_drawdown(db, sample(85_000), now=T0 + timedelta(minutes=1))

    assert outcome.status == "tripped"  # 15.0% against a 15.0% limit: reaching it trips
    async with db_session_factory() as db:
        state = await get_kill_switch_state(db, "paper")
        audit = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "kill_switch.tripped")))
            .scalars()
            .all()
        )
        snapshot = (await _snapshots(db))[-1]
    assert state.tripped is True
    assert state.tripped_at == T0 + timedelta(minutes=1)
    assert state.updated_at == T0 + timedelta(minutes=1)
    assert state.last_drawdown_pct == pytest.approx(15.0)
    assert state.trip_reason == (
        "drawdown 15.00% from a peak equity of 100,000.00 (now 85,000.00) reached the 15.00% limit"
    )
    assert len(audit) == 1
    assert audit[0].actor == MONITOR_ACTOR == "system:drawdown-monitor"
    assert audit[0].entity_type == "kill-switch"
    assert audit[0].entity_id == "paper"
    assert audit[0].details["equity"] == 85_000
    assert audit[0].details["peak_equity"] == 100_000
    assert audit[0].details["threshold_pct"] == 15.0
    assert audit[0].details["snapshot_id"] == str(snapshot.id)
    assert audit[0].details == {
        "reason": state.trip_reason,
        "equity": 85_000,
        "peak_equity": 100_000,
        "drawdown_pct": pytest.approx(15.0),
        "threshold_pct": 15.0,
        "price_source": "real",
        "snapshot_id": str(snapshot.id),
    }
    assert len(alerts) == 1
    assert alerts[0][0] == AlertLevel.KILL_SWITCH
    assert alerts[0][1] == "Kill switch tripped (paper)"
    assert alerts[0][2] == state.trip_reason
    assert alerts[0][3] == {
        "mode": "paper",
        "threshold_pct": 15.0,
        "drawdown_pct": pytest.approx(15.0),
    }


async def test_a_drawdown_just_under_the_limit_does_not_trip(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        outcome = await evaluate_mode_drawdown(db, sample(85_001), now=T0 + timedelta(minutes=1))

    assert outcome.status == "ok"


async def test_the_switch_stays_latched_and_trips_only_once(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        await evaluate_mode_drawdown(db, sample(80_000), now=T0 + timedelta(minutes=1))
        again = await evaluate_mode_drawdown(db, sample(70_000), now=T0 + timedelta(minutes=2))
        recovered = await evaluate_mode_drawdown(db, sample(150_000), now=T0 + timedelta(minutes=3))

    assert again.status == "already_tripped"
    assert recovered.status == "already_tripped"  # a full recovery does not clear it
    async with db_session_factory() as db:
        state = await get_kill_switch_state(db, "paper")
        audit = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "kill_switch.tripped")))
            .scalars()
            .all()
        )
    assert state.tripped is True
    assert state.tripped_at == T0 + timedelta(minutes=1)  # the first trip's time, not rewritten
    assert len(audit) == 1
    assert len(alerts) == 1


async def test_the_applied_dual_control_limit_is_the_threshold(db_session_factory, alerts):
    await _apply_limit(db_session_factory, 5.0)
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        outcome = await evaluate_mode_drawdown(db, sample(94_000), now=T0 + timedelta(minutes=1))

    assert outcome.status == "tripped"  # 6% breaches a 5% limit; the 15% default would not
    assert outcome.threshold_pct == 5.0
    async with db_session_factory() as db:
        state = await get_kill_switch_state(db, "paper")
    assert state.threshold_pct == 5.0  # what the console shows is the limit in force


async def test_without_an_applied_limit_the_default_is_used_and_stored(db_session_factory, alerts):
    async with db_session_factory() as db:
        outcome = await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        state = await get_kill_switch_state(db, "paper")

    assert outcome.threshold_pct == 15.0
    assert state.threshold_pct == 15.0


async def test_a_reading_that_could_not_be_measured_is_stored_as_a_gap_and_judged_as_nothing(
    db_session_factory, alerts
):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        await evaluate_mode_drawdown(db, sample(90_000), now=T0 + timedelta(minutes=1))
        outcome = await evaluate_mode_drawdown(
            db, sample(None, note="1 open position(s) have no price"), now=T0 + timedelta(minutes=2)
        )

    assert outcome.status == "cannot_evaluate"
    assert outcome.drawdown_pct is None
    assert outcome.peak_equity == 100_000
    async with db_session_factory() as db:
        state = await get_kill_switch_state(db, "paper")
        rows = await _snapshots(db)
    assert state.tripped is False
    assert state.updated_at == T0 + timedelta(minutes=2)  # the gap is still a reading
    assert state.last_drawdown_pct == pytest.approx(10.0)  # the gap did not overwrite it
    gap = rows[-1]
    assert gap.equity is None
    assert gap.note == "1 open position(s) have no price"
    assert gap.peak_equity == 100_000
    assert gap.drawdown_pct is None
    assert alerts == []


async def test_a_gap_never_counts_toward_the_peak(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(None), now=T0)
        outcome = await evaluate_mode_drawdown(db, sample(50_000), now=T0 + timedelta(minutes=1))

    assert outcome.peak_equity == 50_000


async def test_a_human_reset_rebases_the_peak(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        await evaluate_mode_drawdown(db, sample(80_000), now=T0 + timedelta(minutes=1))
    async with db_session_factory() as db:
        await reset_kill_switch(db, "paper", reset_by="rm", reason="checked")
    after = datetime.now(UTC) + timedelta(seconds=1)

    async with db_session_factory() as db:
        outcome = await evaluate_mode_drawdown(db, sample(80_000), now=after)

    # Measured against the old 100,000 peak this would be the same 20% that just tripped it.
    assert outcome.status == "ok"
    assert outcome.peak_equity == 80_000
    assert outcome.drawdown_pct == 0.0


async def test_the_two_modes_never_share_a_reading_or_a_trip(db_session_factory, alerts):
    async with db_session_factory() as db:
        await evaluate_mode_drawdown(db, sample(100_000), now=T0)
        await evaluate_mode_drawdown(db, sample(50_000), now=T0 + timedelta(minutes=1))
        live = await evaluate_mode_drawdown(db, sample(10_000, mode="live"), now=T0)

    assert live.status == "ok"
    assert live.peak_equity == 10_000  # the paper high of 100,000 is not live's
    async with db_session_factory() as db:
        paper_state = await get_kill_switch_state(db, "paper")
        live_state = await get_kill_switch_state(db, "live")
        live_rows = await _snapshots(db, "live")
    assert paper_state.tripped is True
    assert live_state.tripped is False
    assert len(live_rows) == 1


async def test_a_peak_of_zero_or_less_cannot_trip(db_session_factory, alerts):
    async with db_session_factory() as db:
        outcome = await evaluate_mode_drawdown(db, sample(-5_000), now=T0)

    assert outcome.status == "ok"
    assert outcome.drawdown_pct is None  # nothing meaningful to divide by, not a fabricated 0


async def test_an_outcome_is_an_immutable_value(db_session_factory, alerts):
    from dataclasses import FrozenInstanceError

    async with db_session_factory() as db:
        outcome = await evaluate_mode_drawdown(db, sample(100_000), now=T0)

    with pytest.raises(FrozenInstanceError):
        outcome.status = "tripped"  # type: ignore[misc]
    assert not hasattr(outcome, "__dict__")  # slots: no stray attributes
