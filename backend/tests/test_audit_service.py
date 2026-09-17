"""src/audit/service.py: the advisory-lock-serialized writer, plus the
DB-level append-only trigger this phase's acceptance criteria explicitly
calls out ("an attempted UPDATE/DELETE against the audit_log table fails
at the DB level"). The trigger itself is created by an `after_create`
event listener on `AuditLog.__table__` (src/models/audit_log.py) so it
exists even in a test DB built via `Base.metadata.create_all` (conftest's
db_session_factory fixture), not just via the real Alembic migration --
see that module's comment for why both paths share one SQL constant.
"""

import asyncio

import pytest
import structlog
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from src.audit.chain import GENESIS_HASH
from src.audit.service import verify_db_chain, write_audit_entry
from src.models.audit_log import AuditLog


async def test_write_audit_entry_assigns_sequential_numbers(db_session_factory):
    async with db_session_factory() as db:
        e1 = await write_audit_entry(db, actor="alice", action="widget.created")
        await db.commit()
    async with db_session_factory() as db:
        e2 = await write_audit_entry(db, actor="bob", action="widget.updated")
        await db.commit()

    assert e1.sequence == 1
    assert e2.sequence == 2
    assert e1.previous_hash == GENESIS_HASH
    assert e2.previous_hash == e1.hash


async def test_write_audit_entry_sequence_is_gap_free_under_concurrency(db_session_factory):
    async def _write(i: int) -> None:
        async with db_session_factory() as db:
            await write_audit_entry(db, actor=f"actor-{i}", action="concurrent.write")
            await db.commit()

    await asyncio.gather(*(_write(i) for i in range(10)))

    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()

    assert [row.sequence for row in rows] == list(range(1, 11))
    for i in range(1, len(rows)):
        assert rows[i].previous_hash == rows[i - 1].hash


async def test_write_audit_entry_picks_up_bound_correlation_id(db_session_factory):
    tokens = structlog.contextvars.bind_contextvars(correlation_id="corr-abc-123")
    try:
        async with db_session_factory() as db:
            entry = await write_audit_entry(db, actor="alice", action="widget.created")
            await db.commit()
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    assert entry.correlation_id == "corr-abc-123"


async def test_write_audit_entry_explicit_correlation_id_overrides_bound_one(db_session_factory):
    tokens = structlog.contextvars.bind_contextvars(correlation_id="bound-id")
    try:
        async with db_session_factory() as db:
            entry = await write_audit_entry(
                db, actor="alice", action="widget.created", correlation_id="explicit-id"
            )
            await db.commit()
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    assert entry.correlation_id == "explicit-id"


async def test_write_audit_entry_no_bound_correlation_id_is_none(db_session_factory):
    async with db_session_factory() as db:
        entry = await write_audit_entry(db, actor="alice", action="widget.created")
        await db.commit()

    assert entry.correlation_id is None


async def test_verify_db_chain_valid_for_untampered_rows(db_session_factory):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await write_audit_entry(db, actor="carol", action="c")
        await db.commit()

    async with db_session_factory() as db:
        result = await verify_db_chain(db)

    assert result.valid is True
    assert result.entries_checked == 3


async def test_verify_db_chain_detects_a_directly_tampered_row(db_session_factory):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await db.commit()

    # The append-only trigger (tested directly below) genuinely blocks a
    # raw UPDATE, so tampering here requires first dropping it -- exactly
    # the "a superuser drops and later restores the trigger to sneak an
    # UPDATE through" scenario src.orchestration.audit_scheduler's module
    # docstring names as the reason verify_db_chain exists at all: an
    # independent, content-based check that doesn't rely on the trigger
    # having stayed in place.
    async with db_session_factory() as db:
        await db.execute(text("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log"))
        await db.execute(
            text("UPDATE audit_log SET actor = 'mallory' WHERE sequence = 1").execution_options(
                synchronize_session=False
            )
        )
        await db.commit()

    async with db_session_factory() as db:
        result = await verify_db_chain(db)

    assert result.valid is False
    assert result.first_broken_sequence == 1


async def test_db_trigger_rejects_update_against_audit_log(db_session_factory):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await db.commit()

    async with db_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text("UPDATE audit_log SET actor = 'mallory' WHERE sequence = 1"))
            await db.commit()


async def test_db_trigger_rejects_delete_against_audit_log(db_session_factory):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await db.commit()

    async with db_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(delete(AuditLog).where(AuditLog.sequence == 1))
            await db.commit()

    # Confirm the row genuinely survived -- a rejected statement inside a
    # transaction that somehow still committed would be just as bad as no
    # trigger at all.
    async with db_session_factory() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor == "alice"
