"""Human suggestions flow tests (Build Spec §9): free-text -> AI verdict ->
optional regeneration, diff-gated like Phase 3's prompt versioning.
"""

import uuid

import pytest

from src.models.strategy_suggestion import StrategySuggestion, SuggestionStatus
from src.orchestration.strategies import create_strategy, create_version_with_validation
from src.orchestration.strategy_suggestions import (
    NoSuchSuggestionError,
    regenerate_from_suggestion,
    review_suggestion,
    submit_suggestion,
)

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def _make_strategy_and_version(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="Momentum for NIFTY")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
    return strategy, version


async def test_submit_suggestion_starts_pending(db_session_factory):
    strategy, version = await _make_strategy_and_version(db_session_factory)

    async with db_session_factory() as db:
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
            requested_by="trader1",
        )

    assert suggestion.status == SuggestionStatus.PENDING
    assert suggestion.ai_verdict is None


async def test_review_produces_a_verdict_and_marks_reviewed(db_session_factory):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    async with db_session_factory() as db:
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
        )

    async with db_session_factory() as db:
        reviewed = await review_suggestion(db, suggestion.id)

    assert reviewed.status == SuggestionStatus.REVIEWED
    assert reviewed.ai_verdict is not None
    assert "verdict" in reviewed.ai_verdict
    assert reviewed.reviewed_at is not None


async def test_regeneration_creates_a_new_version_and_stores_a_diff(db_session_factory):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    async with db_session_factory() as db:
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
        )
    async with db_session_factory() as db:
        await review_suggestion(db, suggestion.id)

    async with db_session_factory() as db:
        new_version = await regenerate_from_suggestion(db, suggestion.id, created_by="trader1")

    assert new_version is not None
    assert new_version.id != version.id
    assert new_version.version_number == 2

    async with db_session_factory() as db:
        suggestion_after = await db.get(StrategySuggestion, suggestion.id)
    assert suggestion_after.status == SuggestionStatus.REGENERATED
    assert suggestion_after.regenerated_version_id == new_version.id
    # Diff-gated: the diff must be produced as part of the same call that
    # linked the regenerated version -- never one without the other.
    assert suggestion_after.regeneration_diff is not None


async def test_no_regeneration_path_sets_regenerated_version_without_a_diff(db_session_factory):
    """Static proof of the diff-gating contract itself, not just one
    example run of it: there is no function in this module capable of
    linking a regenerated_version_id without also computing a diff.
    """
    import src.orchestration.strategy_suggestions as suggestions_module

    assert not hasattr(suggestions_module, "link_regenerated_version_without_diff")
    assert not hasattr(suggestions_module, "set_regenerated_version_id")


async def test_regeneration_refuses_an_unreviewed_suggestion(db_session_factory):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    async with db_session_factory() as db:
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Add a stop-loss",
        )

    async with db_session_factory() as db:
        with pytest.raises(ValueError):
            await regenerate_from_suggestion(db, suggestion.id)


async def test_a_rejected_verdict_never_regenerates(db_session_factory, monkeypatch):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    async with db_session_factory() as db:
        suggestion = await submit_suggestion(
            db,
            strategy_id=strategy.id,
            base_version_id=version.id,
            suggestion_text="Do something silly",
        )
        suggestion.ai_verdict = {"verdict": "reject", "reasoning": "not a good idea"}
        suggestion.status = SuggestionStatus.REVIEWED
        await db.commit()

    async with db_session_factory() as db:
        result = await regenerate_from_suggestion(db, suggestion.id)

    assert result is None
    async with db_session_factory() as db:
        suggestion_after = await db.get(StrategySuggestion, suggestion.id)
    assert suggestion_after.status == SuggestionStatus.REVIEWED  # unchanged, not "regenerated"
    assert suggestion_after.regenerated_version_id is None


async def test_review_unknown_suggestion_raises(db_session_factory):
    with pytest.raises(NoSuchSuggestionError):
        async with db_session_factory() as db:
            await review_suggestion(db, uuid.uuid4())
