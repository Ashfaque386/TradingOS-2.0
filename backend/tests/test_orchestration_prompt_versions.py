"""Prompt versioning tests (Build Spec §7.1): immutable content, diff-gated
activation, rollback.
"""

import uuid

import pytest
from sqlalchemy import select

from src.models.agent_identity import AgentIdentity
from src.models.audit_log import AuditLog
from src.models.prompt_version import PromptVersion, PromptVersionStatus
from src.orchestration.prompt_versions import (
    NoSuchPromptVersionError,
    activate_prompt_version,
    create_prompt_version,
    get_active_prompt_content,
    get_active_prompts,
    rollback_prompt_version,
)


async def _seed_identity(db_session_factory, agent_id: str = "ceo-agent") -> None:
    async with db_session_factory() as db:
        db.add(AgentIdentity(agent_id=agent_id, name="CEO Agent"))
        await db.commit()


async def test_create_prompt_version_starts_as_draft_with_incrementing_numbers(
    db_session_factory,
):
    await _seed_identity(db_session_factory)

    async with db_session_factory() as db:
        v1 = await create_prompt_version(db, agent_id="ceo-agent", content="You are the CEO.")
    async with db_session_factory() as db:
        v2 = await create_prompt_version(db, agent_id="ceo-agent", content="You are the CEO v2.")

    assert v1.version_number == 1
    assert v2.version_number == 2
    assert v1.status == PromptVersionStatus.DRAFT
    assert v2.status == PromptVersionStatus.DRAFT


async def test_activate_supersedes_previous_active_and_records_diff(db_session_factory):
    await _seed_identity(db_session_factory)

    async with db_session_factory() as db:
        v1 = await create_prompt_version(db, agent_id="ceo-agent", content="line one\n")
    async with db_session_factory() as db:
        v1 = await activate_prompt_version(db, agent_id="ceo-agent", version_id=v1.id)

    assert v1.status == PromptVersionStatus.ACTIVE
    assert v1.activated_at is not None
    assert v1.diff_from_previous is None  # nothing was active before it

    async with db_session_factory() as db:
        v2 = await create_prompt_version(db, agent_id="ceo-agent", content="line one\nline two\n")
    async with db_session_factory() as db:
        v2 = await activate_prompt_version(db, agent_id="ceo-agent", version_id=v2.id)

    assert v2.status == PromptVersionStatus.ACTIVE
    assert v2.diff_from_previous
    assert "line two" in v2.diff_from_previous

    async with db_session_factory() as db:
        v1_after = await db.get(PromptVersion, v1.id)
    assert v1_after.status == PromptVersionStatus.SUPERSEDED


def test_content_is_immutable_no_update_function_exists():
    import src.orchestration.prompt_versions as pv_module

    assert not hasattr(pv_module, "update_prompt_version_content")
    assert not hasattr(pv_module, "edit_prompt_version")


async def test_rollback_reactivates_an_older_superseded_version(db_session_factory):
    await _seed_identity(db_session_factory)

    async with db_session_factory() as db:
        v1 = await create_prompt_version(db, agent_id="ceo-agent", content="v1")
    async with db_session_factory() as db:
        v1 = await activate_prompt_version(db, agent_id="ceo-agent", version_id=v1.id)

    async with db_session_factory() as db:
        v2 = await create_prompt_version(db, agent_id="ceo-agent", content="v2")
    async with db_session_factory() as db:
        await activate_prompt_version(db, agent_id="ceo-agent", version_id=v2.id)

    async with db_session_factory() as db:
        rolled_back = await rollback_prompt_version(
            db, agent_id="ceo-agent", target_version_id=v1.id
        )

    assert rolled_back.status == PromptVersionStatus.ACTIVE
    assert rolled_back.id == v1.id

    async with db_session_factory() as db:
        v2_after = await db.get(PromptVersion, v2.id)
    assert v2_after.status == PromptVersionStatus.SUPERSEDED


async def test_activate_unknown_version_raises(db_session_factory):
    await _seed_identity(db_session_factory)

    with pytest.raises(NoSuchPromptVersionError):
        async with db_session_factory() as db:
            await activate_prompt_version(db, agent_id="ceo-agent", version_id=uuid.uuid4())


async def test_activate_version_belonging_to_a_different_agent_raises(db_session_factory):
    await _seed_identity(db_session_factory, "ceo-agent")
    await _seed_identity(db_session_factory, "risk-manager")

    async with db_session_factory() as db:
        v1 = await create_prompt_version(db, agent_id="ceo-agent", content="v1")

    with pytest.raises(NoSuchPromptVersionError):
        async with db_session_factory() as db:
            await activate_prompt_version(db, agent_id="risk-manager", version_id=v1.id)


async def test_create_and_activate_both_write_a_real_audit_entry(db_session_factory):
    """Phase 19 (docs/phase19-audit.md Part 2.2): the audit found
    prompt-version create/activate was the one Agent Fleet mutation not
    captured in the hash-chained audit log."""
    await _seed_identity(db_session_factory)

    async with db_session_factory() as db:
        v1 = await create_prompt_version(
            db, agent_id="ceo-agent", content="v1", created_by="ops@example.com"
        )
        await activate_prompt_version(
            db, agent_id="ceo-agent", version_id=v1.id, actor="ops@example.com"
        )

        rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()

    actions = [row.action for row in rows]
    assert "prompt_version.created" in actions
    assert "prompt_version.activated" in actions
    activated_row = next(r for r in rows if r.action == "prompt_version.activated")
    assert activated_row.actor == "ops@example.com"
    assert activated_row.entity_id == str(v1.id)


async def test_get_active_prompt_content_is_none_until_activated(db_session_factory):
    """Phase 19: the exact "no fabrication, no effect until activated"
    contract every real LLM call site now relies on."""
    await _seed_identity(db_session_factory)

    async with db_session_factory() as db:
        assert await get_active_prompt_content(db, "ceo-agent") is None

        v1 = await create_prompt_version(
            db, agent_id="ceo-agent", content="You are a cautious CEO."
        )
        assert await get_active_prompt_content(db, "ceo-agent") is None  # still draft, not active

        await activate_prompt_version(db, agent_id="ceo-agent", version_id=v1.id)
        assert await get_active_prompt_content(db, "ceo-agent") == "You are a cautious CEO."


async def test_get_active_prompts_batch_only_returns_agents_with_an_active_version(
    db_session_factory,
):
    await _seed_identity(db_session_factory, "ceo-agent")
    await _seed_identity(db_session_factory, "risk-manager")

    async with db_session_factory() as db:
        v1 = await create_prompt_version(db, agent_id="ceo-agent", content="ceo prompt")
        await activate_prompt_version(db, agent_id="ceo-agent", version_id=v1.id)

        result = await get_active_prompts(db, ["ceo-agent", "risk-manager", "strategy-generator"])

    assert result == {"ceo-agent": "ceo prompt"}
