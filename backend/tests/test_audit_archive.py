"""WORM archive tests (Build Spec §19): appending, and the
chain-divergence check correctly flagging a deliberately tampered archive
copy -- this phase's explicit acceptance criterion.
"""

import json
import subprocess

from src.audit.archive import (
    append_entries_to_archive,
    last_archived_sequence,
    read_all_archived_entries,
    run_archival_sweep,
    verify_archive_chain_divergence,
)
from src.audit.service import write_audit_entry


async def test_run_archival_sweep_writes_new_rows_and_is_idempotent(db_session_factory, tmp_path):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await db.commit()

    async with db_session_factory() as db:
        first = await run_archival_sweep(db, tmp_path)
    assert first.entries_archived == 2
    assert last_archived_sequence(tmp_path) == 2

    # A second sweep with no new rows archives nothing more.
    async with db_session_factory() as db:
        second = await run_archival_sweep(db, tmp_path)
    assert second.entries_archived == 0

    entries = read_all_archived_entries(tmp_path)
    assert [e.sequence for e in entries] == [1, 2]
    assert [e.actor for e in entries] == ["alice", "bob"]


async def test_run_archival_sweep_only_archives_new_rows_since_last_run(
    db_session_factory, tmp_path
):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await db.commit()
    async with db_session_factory() as db:
        await run_archival_sweep(db, tmp_path)

    async with db_session_factory() as db:
        await write_audit_entry(db, actor="bob", action="b")
        await db.commit()
    async with db_session_factory() as db:
        result = await run_archival_sweep(db, tmp_path)

    assert result.entries_archived == 1
    assert last_archived_sequence(tmp_path) == 2


async def test_verify_archive_chain_divergence_empty_archive_is_not_diverged(tmp_path):
    # No DB session needed at all -- an empty archive short-circuits before
    # ever touching the DB (see src/audit/archive.py's early return).
    class _NeverCalledSession:
        async def execute(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("should not query the DB for an empty archive")

    result = await verify_archive_chain_divergence(_NeverCalledSession(), tmp_path)
    assert result.diverged is False
    assert result.reason is not None


async def test_verify_archive_chain_divergence_valid_archive_matches_live_db(
    db_session_factory, tmp_path
):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await db.commit()
    async with db_session_factory() as db:
        await run_archival_sweep(db, tmp_path)

    async with db_session_factory() as db:
        result = await verify_archive_chain_divergence(db, tmp_path)

    assert result.diverged is False
    assert result.archive_internally_valid is True
    assert result.live_db_matches_archive is True


def _clear_append_only(path) -> None:
    """`append_entries_to_archive` attempts `chattr +a` on first creation
    (src/audit/archive.py) -- and it genuinely succeeds in this sandbox,
    so a tamper test must first clear that OS-level attribute before it
    can rewrite the file at all, exactly like a real attacker would need
    root plus CAP_LINUX_IMMUTABLE to bypass it. This is simulating that
    bypass to prove the *detection* logic, not defeating the protection
    itself -- the module's own docstring is explicit that the OS attribute
    is defense in depth, not the sole guarantee.
    """
    subprocess.run(["chattr", "-a", str(path)], capture_output=True, check=False)


async def test_verify_archive_chain_divergence_flags_a_tampered_archive_copy(
    db_session_factory, tmp_path
):
    """The explicit acceptance criterion: tamper with the archive's own
    on-disk NDJSON file (not the live DB) and confirm the divergence check
    catches it and reports the exact sequence where it starts.
    """
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await write_audit_entry(db, actor="carol", action="c")
        await db.commit()
    async with db_session_factory() as db:
        await run_archival_sweep(db, tmp_path)

    archive_path = tmp_path / "audit_log.ndjson"
    _clear_append_only(archive_path)
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # Tamper with sequence 2's archived actor, leaving its stored hash
    # untouched -- exactly what an attacker editing a file in place would
    # produce (unless they also recompute a fresh hash chain from that
    # point forward, which is a separate, still-detected failure mode
    # covered by the "archive internally invalid" branch below).
    tampered = json.loads(lines[1])
    assert tampered["sequence"] == 2
    tampered["actor"] = "mallory"
    lines[1] = json.dumps(tampered, sort_keys=True)
    archive_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async with db_session_factory() as db:
        result = await verify_archive_chain_divergence(db, tmp_path)

    assert result.diverged is True
    assert result.first_diverged_sequence == 2


async def test_verify_archive_chain_divergence_flags_a_broken_internal_archive_chain(
    db_session_factory, tmp_path
):
    """A different tamper shape: rewrite an archived row's `hash` field
    itself, breaking the archive's own internal chain (previous_hash of
    the next row no longer matches) before it's even compared to the live
    DB.
    """
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="alice", action="a")
        await write_audit_entry(db, actor="bob", action="b")
        await db.commit()
    async with db_session_factory() as db:
        await run_archival_sweep(db, tmp_path)

    archive_path = tmp_path / "audit_log.ndjson"
    _clear_append_only(archive_path)
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["hash"] = "f" * 64
    lines[0] = json.dumps(first, sort_keys=True)
    archive_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async with db_session_factory() as db:
        result = await verify_archive_chain_divergence(db, tmp_path)

    assert result.diverged is True
    assert result.archive_internally_valid is False


async def test_append_entries_to_archive_no_entries_is_a_no_op(tmp_path):
    result = append_entries_to_archive(tmp_path, [])
    assert result.entries_archived == 0
    assert not (tmp_path / "audit_log.ndjson").exists()
