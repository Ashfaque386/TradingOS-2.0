"""Prompt versioning tests (Build Spec §7.1): immutable content, diff-gated
activation, rollback.
"""

import uuid

import pytest

from src.models.agent_identity import AgentIdentity
from src.models.prompt_version import PromptVersion, PromptVersionStatus
from src.orchestration.prompt_versions import (
    NoSuchPromptVersionError,
    activate_prompt_version,
    create_prompt_version,
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
