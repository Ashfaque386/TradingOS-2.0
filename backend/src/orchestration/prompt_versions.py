"""Prompt versioning (Build Spec §7.1): immutable content per version,
diff-gated activation, rollback.

Immutability: this module has exactly one function that writes `content`
(create_prompt_version, on INSERT) -- there is no update-content function
anywhere here. A revision is retired by activating a different row
(activate_prompt_version), never edited in place.

Diff-gated activation: activate_prompt_version() always computes the
unified diff between the version being activated and whichever version was
previously ACTIVE for that agent, and stores it on the newly-active row as
part of the same call that flips status -- there is no path that activates
a version without that diff having been produced first.

Rollback is the same call, targeting an older (SUPERSEDED) version's id --
re-activating it computes a fresh diff against the *current* active version
and supersedes that one, same as any other activation.
"""

import difflib
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.models.prompt_version import PromptVersion, PromptVersionStatus


class NoSuchPromptVersionError(Exception):
    pass


async def create_prompt_version(
    db: AsyncSession, *, agent_id: str, content: str, created_by: str | None = None
) -> PromptVersion:
    next_number = (
        await db.execute(
            select(func.coalesce(func.max(PromptVersion.version_number), 0) + 1).where(
                PromptVersion.agent_id == agent_id
            )
        )
    ).scalar_one()

    version = PromptVersion(
        agent_id=agent_id,
        version_number=next_number,
        content=content,
        status=PromptVersionStatus.DRAFT,
        created_by=created_by,
    )
    db.add(version)
    await db.flush()
    # Phase 19 (docs/phase19-audit.md Part 2.2): the audit found this
    # module was the one Agent Fleet mutation not captured in the
    # hash-chained audit log, unlike Identity/Skills/Heartbeat/
    # Enable-Disable, all of which go through apply_config_text's own
    # audit write.
    await write_audit_entry(
        db,
        actor=created_by or "unknown",
        action="prompt_version.created",
        entity_type="prompt_version",
        entity_id=str(version.id),
        details={"agent_id": agent_id, "version_number": next_number},
    )
    await db.commit()
    await db.refresh(version)
    return version


async def _get_active(db: AsyncSession, agent_id: str) -> PromptVersion | None:
    result = await db.execute(
        select(PromptVersion).where(
            PromptVersion.agent_id == agent_id, PromptVersion.status == PromptVersionStatus.ACTIVE
        )
    )
    return result.scalar_one_or_none()


async def get_active_prompt_content(db: AsyncSession, agent_id: str) -> str | None:
    """Phase 19 (docs/phase19-audit.md Part 2.2's central finding): the
    real consumption point this module previously had none of. Returns
    the ACTIVE version's `content` for `agent_id`, or `None` if no
    version has ever been activated for it -- every real LLM call site
    (`src.agents.graph`'s 3 LLM-backed nodes,
    `src.orchestration.strategies.generate_strategy_code`,
    `src.orchestration.strategy_suggestions.review_suggestion`) treats
    `None` as "keep using the existing hardcoded prompt," so activating a
    version is what actually changes behavior -- an agent with no
    activated version yet behaves exactly as before this wiring existed.
    """
    active = await _get_active(db, agent_id)
    return active.content if active is not None else None


async def get_active_prompts(db: AsyncSession, agent_ids: list[str]) -> dict[str, str]:
    """Batch form of `get_active_prompt_content` -- only agent ids with a
    real active version appear in the returned dict."""
    result = await db.execute(
        select(PromptVersion.agent_id, PromptVersion.content).where(
            PromptVersion.agent_id.in_(agent_ids),
            PromptVersion.status == PromptVersionStatus.ACTIVE,
        )
    )
    return dict(result.all())


def _unified_diff(old: PromptVersion, new: PromptVersion) -> str:
    return "".join(
        difflib.unified_diff(
            old.content.splitlines(keepends=True),
            new.content.splitlines(keepends=True),
            fromfile=f"v{old.version_number}",
            tofile=f"v{new.version_number}",
        )
    )


async def activate_prompt_version(
    db: AsyncSession, *, agent_id: str, version_id: uuid.UUID, actor: str | None = None
) -> PromptVersion:
    target = await db.get(PromptVersion, version_id)
    if target is None or target.agent_id != agent_id:
        raise NoSuchPromptVersionError(f"no prompt version {version_id} for agent {agent_id!r}")

    current_active = await _get_active(db, agent_id)
    diff_text = ""
    if current_active is not None and current_active.id != target.id:
        diff_text = _unified_diff(current_active, target)
        await db.execute(
            update(PromptVersion)
            .where(
                PromptVersion.id == current_active.id,
                PromptVersion.status == PromptVersionStatus.ACTIVE,
            )
            .values(status=PromptVersionStatus.SUPERSEDED)
        )

    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.id == target.id)
        .values(
            status=PromptVersionStatus.ACTIVE,
            diff_from_previous=diff_text or None,
            activated_at=func.now(),
        )
    )
    await write_audit_entry(
        db,
        actor=actor or "unknown",
        action="prompt_version.activated",
        entity_type="prompt_version",
        entity_id=str(target.id),
        details={"agent_id": agent_id, "version_number": target.version_number},
    )
    await db.commit()
    await db.refresh(target)
    return target


async def rollback_prompt_version(
    db: AsyncSession, *, agent_id: str, target_version_id: uuid.UUID
) -> PromptVersion:
    """Rollback is activation of a specific (typically SUPERSEDED) earlier
    version -- same diff-gating, same status-flip mechanics.
    """
    return await activate_prompt_version(db, agent_id=agent_id, version_id=target_version_id)
