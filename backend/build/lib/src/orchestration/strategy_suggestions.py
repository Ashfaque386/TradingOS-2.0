"""Human suggestions flow (Build Spec §9): free-text improvement request ->
AI review verdict -> optional regeneration, diff-gated the same way as
Phase 3's prompt versioning (src.orchestration.prompt_versions) -- a
regeneration always computes and stores a unified diff against the base
version's code as part of the same call that creates the new
StrategyVersion and links it back to the suggestion; there's no path that
regenerates without that diff having been produced.
"""

import difflib
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.models.strategy import Strategy
from src.models.strategy_suggestion import StrategySuggestion, SuggestionStatus
from src.models.strategy_version import StrategyVersion
from src.orchestration.post_trade_review import latest_finding_for_strategy
from src.orchestration.prompt_versions import get_active_prompt_content
from src.orchestration.strategies import create_version_with_validation, generate_strategy_code


class NoSuchSuggestionError(Exception):
    pass


async def submit_suggestion(
    db: AsyncSession,
    *,
    strategy_id: uuid.UUID,
    base_version_id: uuid.UUID,
    suggestion_text: str,
    requested_by: str | None = None,
) -> StrategySuggestion:
    suggestion = StrategySuggestion(
        strategy_id=strategy_id,
        base_version_id=base_version_id,
        suggestion_text=suggestion_text,
        requested_by=requested_by,
        status=SuggestionStatus.PENDING,
    )
    db.add(suggestion)
    await db.commit()
    await db.refresh(suggestion)
    return suggestion


def _fallback_verdict(suggestion_text: str) -> dict:
    return {
        "verdict": "needs_changes",
        "reasoning": (
            "fallback verdict (no LLM provider reachable): treating every "
            "suggestion as needing changes so a human reviewer sees it "
            "rather than having it silently accepted or rejected"
        ),
        "source": "fallback",
    }


async def review_suggestion(
    db: AsyncSession, suggestion_id: uuid.UUID, *, router: LlmRouter | None = None
) -> StrategySuggestion:
    suggestion = await db.get(StrategySuggestion, suggestion_id)
    if suggestion is None:
        raise NoSuchSuggestionError(f"no such suggestion: {suggestion_id}")

    base_version = await db.get(StrategyVersion, suggestion.base_version_id)

    # Phase 19 (docs/phase19-audit.md Part 2.4): the real "next cycle" the
    # Post-Trade Review Agent's findings feed into -- if this strategy has
    # a recent real trade-review finding, fold its commentary into the
    # verdict prompt so a real, already-observed trading outcome informs
    # the review, not just the raw code diff.
    review_context = ""
    finding = await latest_finding_for_strategy(db, suggestion.strategy_id)
    if finding is not None:
        review_context = (
            f"\n\nMost recent post-trade review for this strategy "
            f"({finding.review_date.isoformat()}, {finding.mode}): {finding.commentary}"
        )

    # Phase 19 (docs/phase19-audit.md Part 2.2): the real effect of
    # activating a prompt version for "strategy-generator" -- prefixed
    # ahead of the task-specific instruction below, same posture as
    # src.agents.graph's _with_active_prompt.
    active_prompt = await get_active_prompt_content(db, "strategy-generator")
    task_prompt = (
        f"Review this improvement suggestion for a trading strategy and give a "
        f"verdict (accept/reject/needs_changes) with reasoning.\n"
        f"Current code:\n{base_version.code}\n\n"
        f"Suggestion: {suggestion.suggestion_text}{review_context}"
    )
    prompt = f"{active_prompt}\n\n{task_prompt}" if active_prompt else task_prompt

    router = router or get_llm_router()
    try:
        result = await router.complete(agent_id="strategy-generator", prompt=prompt)
        verdict = {"verdict": "needs_changes", "reasoning": result.text, "source": "llm"}
    except LlmRouterExhaustedError:
        verdict = _fallback_verdict(suggestion.suggestion_text)

    suggestion.ai_verdict = verdict
    suggestion.status = SuggestionStatus.REVIEWED
    suggestion.reviewed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(suggestion)
    return suggestion


async def regenerate_from_suggestion(
    db: AsyncSession,
    suggestion_id: uuid.UUID,
    *,
    router: LlmRouter | None = None,
    created_by: str | None = None,
) -> StrategyVersion | None:
    """Only proceeds for a suggestion whose AI verdict isn't a flat
    "reject" -- a rejected suggestion never produces a new version.
    Regeneration always computes and stores a diff against the base
    version's code as part of this same call (diff-gated, matching
    prompt_versions.activate_prompt_version()'s contract).
    """
    suggestion = await db.get(StrategySuggestion, suggestion_id)
    if suggestion is None:
        raise NoSuchSuggestionError(f"no such suggestion: {suggestion_id}")
    if suggestion.ai_verdict is None:
        raise ValueError(f"suggestion {suggestion_id} has not been reviewed yet")
    if suggestion.ai_verdict.get("verdict") == "reject":
        return None

    strategy = await db.get(Strategy, suggestion.strategy_id)
    base_version = await db.get(StrategyVersion, suggestion.base_version_id)

    new_code = await generate_strategy_code(
        f"{strategy.objective}\n\nIncorporate this suggested change: {suggestion.suggestion_text}",
        router=router,
        db=db,
    )
    diff_text = "".join(
        difflib.unified_diff(
            base_version.code.splitlines(keepends=True),
            new_code.splitlines(keepends=True),
            fromfile=f"v{base_version.version_number}",
            tofile="regenerated",
        )
    )

    new_version = await create_version_with_validation(
        db, strategy, new_code, created_by=created_by
    )

    suggestion.regenerated_version_id = new_version.id
    suggestion.regeneration_diff = diff_text or None
    suggestion.status = SuggestionStatus.REGENERATED
    await db.commit()
    await db.refresh(suggestion)
    await db.refresh(new_version)
    return new_version
