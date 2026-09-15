"""Approval-gate unbypassability (Build Spec §7.3): the gate is checked
inside conditional_transition() itself, not duplicated per-caller — so any
function that performs the guarded transition through that shared
primitive is blocked identically, regardless of which one it is.
"""

import uuid

import pytest

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.orchestration.approvals import create_approval_request, decide_approval_request
from src.orchestration.transitions import (
    ApprovalGate,
    ApprovalRequiredError,
    conditional_transition,
)


async def _make_run(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


def _gate(run_id: uuid.UUID) -> ApprovalGate:
    return ApprovalGate(
        subject_type="organization_run", subject_id=str(run_id), transition_type="demo.pause"
    )


async def _attempt_transition(db, run_id, gate) -> bool:
    return await conditional_transition(
        db,
        table=OrganizationRun.__table__,
        id_column=OrganizationRun.id,
        row_id=run_id,
        status_column=OrganizationRun.status,
        from_status=RunStatus.RUNNING,
        to_status=RunStatus.PAUSED,
        approval=gate,
    )


async def test_gated_transition_blocked_with_no_approval_request(db_session_factory):
    run_id = await _make_run(db_session_factory)
    gate = _gate(run_id)

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _attempt_transition(db, run_id, gate)

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.RUNNING


async def test_gated_transition_blocked_via_a_different_code_path_too(db_session_factory):
    """The unbypassability requirement: a second, independently-written
    caller attempting the exact same gated transition is blocked
    identically -- proving the check lives in conditional_transition()
    itself, not something only the first caller happened to add.
    """
    run_id = await _make_run(db_session_factory)
    gate = _gate(run_id)

    async def _alternate_caller(db) -> bool:
        # A different function, written independently, that also wants to
        # perform this transition -- simulates a future code path reaching
        # the same state change some other way.
        return await conditional_transition(
            db,
            table=OrganizationRun.__table__,
            id_column=OrganizationRun.id,
            row_id=run_id,
            status_column=OrganizationRun.status,
            from_status=RunStatus.RUNNING,
            to_status=RunStatus.PAUSED,
            approval=gate,
        )

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _attempt_transition(db, run_id, gate)

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _alternate_caller(db)

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.RUNNING


async def test_gated_transition_succeeds_once_approved(db_session_factory):
    run_id = await _make_run(db_session_factory)
    gate = _gate(run_id)

    async with db_session_factory() as db:
        request = await create_approval_request(
            db,
            subject_type=gate.subject_type,
            subject_id=gate.subject_id,
            transition_type=gate.transition_type,
            run_id=run_id,
        )

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _attempt_transition(db, run_id, gate)

    async with db_session_factory() as db:
        decided = await decide_approval_request(
            db, request.id, approve=True, decided_by="risk-manager-1"
        )
        assert decided.status.value == "approved"

    async with db_session_factory() as db:
        applied = await _attempt_transition(db, run_id, gate)
        assert applied is True

    async with db_session_factory() as db:
        run = await db.get(OrganizationRun, run_id)
        assert run.status == RunStatus.PAUSED


async def test_rejected_approval_still_blocks(db_session_factory):
    run_id = await _make_run(db_session_factory)
    gate = _gate(run_id)

    async with db_session_factory() as db:
        request = await create_approval_request(
            db,
            subject_type=gate.subject_type,
            subject_id=gate.subject_id,
            transition_type=gate.transition_type,
        )

    async with db_session_factory() as db:
        await decide_approval_request(db, request.id, approve=False, decided_by="risk-manager-1")

    async with db_session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            await _attempt_transition(db, run_id, gate)


async def test_self_confirm_is_not_prevented_at_this_generic_layer(db_session_factory):
    """Documents scope, not a gap: dual-control (a *different* privileged
    user must decide) is a Build Spec §8.5 rule for risk-limit changes,
    not part of this phase's generic ApprovalRequest mechanism. This phase
    only guarantees an approval exists and is APPROVED -- who requested vs
    who decided is a policy a caller can layer on top later.
    """
    run_id = await _make_run(db_session_factory)
    gate = _gate(run_id)

    async with db_session_factory() as db:
        request = await create_approval_request(
            db,
            subject_type=gate.subject_type,
            subject_id=gate.subject_id,
            transition_type=gate.transition_type,
            requested_by="same-user",
        )

    async with db_session_factory() as db:
        await decide_approval_request(db, request.id, approve=True, decided_by="same-user")

    async with db_session_factory() as db:
        applied = await _attempt_transition(db, run_id, gate)
        assert applied is True
