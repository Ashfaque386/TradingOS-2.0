"""Skill Registry tests (Build Spec §16): grant enforcement at the point of
execution -- an ungranted skill call fails even when execute_skill() is
called directly (not through any API route), and there is no mechanism
anywhere in the module to load a skill that isn't already in SKILLS.
"""

import pytest

from src.agents.tools.base import SkillNotFoundError, SkillNotGrantedError
from src.agents.tools.registry import SKILLS, execute_skill
from src.gateway.apply import apply_config_text

CONFIG = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
  agents: {
    defaults: { skills: ['market-data-read'] },
    entries: {
      'options-strategy-agent': { skills: ['option-chain-read'] },
    },
  },
}
"""


async def _apply(db_session_factory) -> None:
    async with db_session_factory() as db:
        await apply_config_text(db, CONFIG, source="test")


async def test_execute_skill_succeeds_for_a_granted_skill(db_session_factory):
    await _apply(db_session_factory)

    result = execute_skill("options-strategy-agent", "option-chain-read")

    assert result["legs"] == []


async def test_execute_skill_fails_for_ungranted_skill_even_called_directly(db_session_factory):
    await _apply(db_session_factory)

    # options-strategy-agent's entry overrides skills to ['option-chain-read']
    # only -- it is NOT granted notification-send. Called directly here,
    # not through any API route, to prove the gate lives in execute_skill()
    # itself.
    with pytest.raises(SkillNotGrantedError):
        execute_skill("options-strategy-agent", "notification-send")


async def test_execute_skill_uses_global_default_grant_when_agent_has_no_override(
    db_session_factory,
):
    await _apply(db_session_factory)

    # market-analyst has no entries override -- falls back to
    # agents.defaults.skills = ['market-data-read'].
    result = execute_skill("market-analyst", "market-data-read")
    assert "note" in result

    with pytest.raises(SkillNotGrantedError):
        execute_skill("market-analyst", "option-chain-read")


def test_execute_skill_rejects_unknown_skill_name():
    with pytest.raises(SkillNotFoundError):
        execute_skill("ceo-agent", "not-a-real-skill")


def test_no_dynamic_skill_loading_mechanism_exists():
    import src.agents.tools.registry as registry_module

    assert not hasattr(registry_module, "register_skill")
    assert not hasattr(registry_module, "load_skill")
    assert set(SKILLS) == {
        "market-data-read",
        "option-chain-read",
        "portfolio-status-read",
        "code-format-lint",
        "sandbox-dry-run",
        "notification-send",
    }
