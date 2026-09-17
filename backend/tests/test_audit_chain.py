"""Pure hash-chain function tests (Build Spec §19): src/audit/chain.py has
no SQLAlchemy/DB dependency, so these run entirely in-memory.
"""

from datetime import UTC, datetime

from src.audit.chain import GENESIS_HASH, AuditChainEntry, compute_entry_hash, verify_chain


def _entry(sequence: int, previous_hash: str, **overrides) -> AuditChainEntry:
    defaults = dict(
        sequence=sequence,
        previous_hash=previous_hash,
        actor="tester",
        action="test.action",
        entity_type="widget",
        entity_id="1",
        details={"k": "v"},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    entry_hash = compute_entry_hash(**defaults)
    return AuditChainEntry(**defaults, hash=entry_hash)


def test_compute_entry_hash_is_deterministic():
    kwargs = dict(
        sequence=1,
        previous_hash=GENESIS_HASH,
        actor="alice",
        action="widget.created",
        entity_type="widget",
        entity_id="42",
        details={"b": 2, "a": 1},
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    assert compute_entry_hash(**kwargs) == compute_entry_hash(**kwargs)


def test_compute_entry_hash_is_order_independent_for_details():
    kwargs = dict(
        sequence=1,
        previous_hash=GENESIS_HASH,
        actor="alice",
        action="widget.created",
        entity_type="widget",
        entity_id="42",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    h1 = compute_entry_hash(details={"a": 1, "b": 2}, **kwargs)
    h2 = compute_entry_hash(details={"b": 2, "a": 1}, **kwargs)
    assert h1 == h2


def test_compute_entry_hash_changes_with_any_field():
    kwargs = dict(
        sequence=1,
        previous_hash=GENESIS_HASH,
        actor="alice",
        action="widget.created",
        entity_type="widget",
        entity_id="42",
        details=None,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    baseline = compute_entry_hash(**kwargs)
    assert compute_entry_hash(**{**kwargs, "actor": "bob"}) != baseline
    assert compute_entry_hash(**{**kwargs, "action": "widget.deleted"}) != baseline
    assert compute_entry_hash(**{**kwargs, "entity_id": "43"}) != baseline
    assert compute_entry_hash(**{**kwargs, "details": {"x": 1}}) != baseline
    assert compute_entry_hash(**{**kwargs, "sequence": 2}) != baseline


def test_verify_chain_empty_is_valid():
    result = verify_chain([])
    assert result.valid is True
    assert result.entries_checked == 0


def test_verify_chain_valid_multi_entry_chain():
    e1 = _entry(1, GENESIS_HASH)
    e2 = _entry(2, e1.hash)
    e3 = _entry(3, e2.hash)
    result = verify_chain([e1, e2, e3])
    assert result.valid is True
    assert result.entries_checked == 3
    assert result.first_broken_sequence is None


def test_verify_chain_detects_broken_previous_hash_link():
    e1 = _entry(1, GENESIS_HASH)
    e2 = _entry(2, "0" * 64)  # wrong -- should have been e1.hash
    result = verify_chain([e1, e2])
    assert result.valid is False
    assert result.first_broken_sequence == 2
    assert "previous_hash" in result.reason


def test_verify_chain_detects_tampered_content():
    e1 = _entry(1, GENESIS_HASH)
    e2 = _entry(2, e1.hash)
    # Tamper with e2's content after the hash was computed -- the stored
    # hash no longer matches what compute_entry_hash would produce.
    tampered = AuditChainEntry(
        sequence=e2.sequence,
        previous_hash=e2.previous_hash,
        actor=e2.actor,
        action="widget.deleted",  # changed from the original "test.action"
        entity_type=e2.entity_type,
        entity_id=e2.entity_id,
        details=e2.details,
        created_at=e2.created_at,
        hash=e2.hash,  # stale hash, computed against the original action
    )
    result = verify_chain([e1, tampered])
    assert result.valid is False
    assert result.first_broken_sequence == 2
    assert "stored hash" in result.reason
