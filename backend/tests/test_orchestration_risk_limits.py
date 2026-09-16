"""Dual-control risk-limit tests (Build Spec §8.5): stage -> confirm (by a
different user) -> apply, with self-confirmation rejected at the
orchestration layer itself (not only relying on an API-level RBAC check).
"""

import uuid

import pytest

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
