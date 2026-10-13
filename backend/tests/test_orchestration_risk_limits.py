"""Dual-control risk-limit tests (Build Spec §8.5): stage -> confirm (by a
different user) -> apply, with self-confirmation rejected at the
orchestration layer itself (not only relying on an API-level RBAC check).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models.risk_limit import RiskLimit
from src.orchestration.risk_limits import (
    NoSuchRiskLimitChangeError,
    RiskLimitChangeNotConfirmedError,
    RiskLimitChangeNotStagedError,
    SelfConfirmationNotAllowedError,
    apply_risk_limit_change,
    confirm_risk_limit_change,
    get_effective_risk_limit,
    stage_risk_limit_change,
)


async def test_effective_limit_falls_back_to_default_before_any_change(db_session_factory):
    async with db_session_factory() as db:
        value = await get_effective_risk_limit(db, "max_drawdown_pct", 15.0)
    assert value == 15.0


async def test_full_stage_confirm_apply_flow_updates_the_effective_value(db_session_factory):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="max_drawdown_pct", proposed_value=10.0, staged_by="user-a"
        )
    assert change.status == "staged"

    async with db_session_factory() as db:
        confirmed = await confirm_risk_limit_change(db, change.id, confirmed_by="user-b")
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by == "user-b"

    async with db_session_factory() as db:
        applied = await apply_risk_limit_change(db, change.id, applied_by="user-b")
    assert applied.status == "applied"

    async with db_session_factory() as db:
        value = await get_effective_risk_limit(db, "max_drawdown_pct", 15.0)
    assert value == 10.0


async def test_self_confirmation_is_rejected(db_session_factory):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="ws_latency_ms", proposed_value=50.0, staged_by="user-a"
        )

    async with db_session_factory() as db:
        with pytest.raises(SelfConfirmationNotAllowedError):
            await confirm_risk_limit_change(db, change.id, confirmed_by="user-a")


async def test_apply_before_confirm_is_rejected(db_session_factory):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="ws_latency_ms", proposed_value=50.0, staged_by="user-a"
        )

    async with db_session_factory() as db:
        with pytest.raises(RiskLimitChangeNotConfirmedError):
            await apply_risk_limit_change(db, change.id, applied_by="user-b")


async def test_confirm_twice_is_rejected(db_session_factory):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="ws_latency_ms", proposed_value=50.0, staged_by="user-a"
        )
    async with db_session_factory() as db:
        await confirm_risk_limit_change(db, change.id, confirmed_by="user-b")

    async with db_session_factory() as db:
        with pytest.raises(RiskLimitChangeNotStagedError):
            await confirm_risk_limit_change(db, change.id, confirmed_by="user-c")


async def test_confirming_an_unknown_change_raises(db_session_factory):
    async with db_session_factory() as db:
        with pytest.raises(NoSuchRiskLimitChangeError):
            await confirm_risk_limit_change(db, uuid.uuid4(), confirmed_by="user-b")


async def test_unapplied_change_never_affects_the_effective_value(db_session_factory):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="max_drawdown_pct", proposed_value=5.0, staged_by="user-a"
        )
    async with db_session_factory() as db:
        await confirm_risk_limit_change(db, change.id, confirmed_by="user-b")

    async with db_session_factory() as db:
        value = await get_effective_risk_limit(db, "max_drawdown_pct", 15.0)
    assert value == 15.0, "confirmed-but-not-applied must not change the effective value"


# ---- Found by mutation testing (2026-10-13) -------------------------------------
# mutmut turned most of the apply step's bookkeeping into no-ops (who applied it,
# when, applying onto a limit that already exists, picking the right limit by
# name) and blanked or reworded the errors, and no test noticed: the existing
# flow only ever applied a first change to an empty table.


async def _stage_confirm(db_session_factory, name: str, value: float, *, staged="user-a"):
    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name=name, proposed_value=value, staged_by=staged
        )
    async with db_session_factory() as db:
        await confirm_risk_limit_change(db, change.id, confirmed_by="user-b")
    return change.id


async def test_applying_a_second_change_updates_the_existing_limit_in_place(db_session_factory):
    first = await _stage_confirm(db_session_factory, "max_drawdown_pct", 10.0)
    async with db_session_factory() as db:
        await apply_risk_limit_change(db, first, applied_by="user-b")
    second = await _stage_confirm(db_session_factory, "max_drawdown_pct", 8.0)
    async with db_session_factory() as db:
        await apply_risk_limit_change(db, second, applied_by="user-c")

    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(RiskLimit).where(RiskLimit.name == "max_drawdown_pct")))
            .scalars()
            .all()
        )
        effective = await get_effective_risk_limit(db, "max_drawdown_pct", 15.0)
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].value == 8.0 and rows[0].updated_by == "user-c"
    assert effective == 8.0


async def test_applying_one_limit_leaves_every_other_limit_alone(db_session_factory):
    drawdown = await _stage_confirm(db_session_factory, "max_drawdown_pct", 10.0)
    async with db_session_factory() as db:
        await apply_risk_limit_change(db, drawdown, applied_by="user-b")
    latency = await _stage_confirm(db_session_factory, "ws_latency_ms", 50.0)
    async with db_session_factory() as db:
        await apply_risk_limit_change(db, latency, applied_by="user-b")

    async with db_session_factory() as db:
        assert await get_effective_risk_limit(db, "max_drawdown_pct", 15.0) == 10.0
        assert await get_effective_risk_limit(db, "ws_latency_ms", 1.0) == 50.0


async def test_the_audit_fields_record_who_did_what_and_when(db_session_factory):
    before = datetime.now(UTC)
    async with db_session_factory() as db:
        staged = await stage_risk_limit_change(
            db, limit_name="max_drawdown_pct", proposed_value=10.0, staged_by="user-a", reason="r"
        )
    async with db_session_factory() as db:
        confirmed = await confirm_risk_limit_change(db, staged.id, confirmed_by="user-b")
    async with db_session_factory() as db:
        applied = await apply_risk_limit_change(db, staged.id, applied_by="user-c")
        limit = (
            await db.execute(select(RiskLimit).where(RiskLimit.name == "max_drawdown_pct"))
        ).scalar_one()

    assert (staged.staged_by, staged.reason) == ("user-a", "r")
    assert confirmed.confirmed_by == "user-b" and confirmed.confirmed_at is not None
    assert applied.applied_by == "user-c" and applied.applied_at is not None
    assert limit.updated_by == "user-c"
    # In order, and all after the test began: nothing is stamped with a stale time.
    assert before <= confirmed.confirmed_at <= applied.applied_at <= datetime.now(UTC)
    assert applied.staged_by == "user-a" and applied.confirmed_by == "user-b"


async def test_the_errors_say_which_change_and_why(db_session_factory):
    missing = uuid.uuid4()
    async with db_session_factory() as db:
        with pytest.raises(NoSuchRiskLimitChangeError) as no_such:
            await apply_risk_limit_change(db, missing, applied_by="user-b")
    assert no_such.value.change_id == missing
    assert str(no_such.value) == f"no risk limit change request {missing}"

    async with db_session_factory() as db:
        change = await stage_risk_limit_change(
            db, limit_name="ws_latency_ms", proposed_value=50.0, staged_by="user-a"
        )
    async with db_session_factory() as db:
        with pytest.raises(SelfConfirmationNotAllowedError) as selfie:
            await confirm_risk_limit_change(db, change.id, confirmed_by="user-a")
    assert selfie.value.change_id == change.id and selfie.value.actor == "user-a"
    assert str(selfie.value) == (
        f"user-a staged risk limit change {change.id} and cannot confirm it too"
    )

    async with db_session_factory() as db:
        with pytest.raises(RiskLimitChangeNotConfirmedError) as unconfirmed:
            await apply_risk_limit_change(db, change.id, applied_by="user-b")
    assert unconfirmed.value.change_id == change.id and unconfirmed.value.status == "staged"
    assert str(unconfirmed.value) == (f"risk limit change {change.id} is 'staged', not 'confirmed'")

    async with db_session_factory() as db:
        await confirm_risk_limit_change(db, change.id, confirmed_by="user-b")
    async with db_session_factory() as db:
        with pytest.raises(RiskLimitChangeNotStagedError) as unstaged:
            await confirm_risk_limit_change(db, change.id, confirmed_by="user-c")
    assert unstaged.value.change_id == change.id and unstaged.value.status == "confirmed"
    assert str(unstaged.value) == f"risk limit change {change.id} is 'confirmed', not 'staged'"
