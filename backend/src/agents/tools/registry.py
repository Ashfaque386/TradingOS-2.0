"""Skill Registry (Build Spec §16): the fixed, in-repo SKILLS map plus
grant enforcement at the point of execution.

Grants are the existing Gateway config surface built in Phase 1 --
AgentDefaultsConfig.skills (global default grant) overridden per agent by
AgentEntryConfig.skills -- already merged into
EffectiveAgentConfig.skills by src.gateway.loader.compute_effective_agents
and kept live by src.gateway.state.GatewayState. execute_skill() is the
single call path every caller (a LangGraph node, a future API route, a
test) goes through, so the grant check can never be bypassed by skipping
some UI/API-layer gate -- there isn't a second path to the skill functions
that doesn't run through it.

There is no function anywhere in this module that registers a skill not
already listed in SKILLS at import time (Build Spec §16: "No external
ingestion path") -- a name not in SKILLS is a SkillNotFoundError, never a
lookup that falls through to loading something.
"""

import inspect

from src.agents.tools.base import SkillFn, SkillNotFoundError, SkillNotGrantedError
from src.agents.tools.code_lint import code_format_lint
from src.agents.tools.market_data import market_data_read
from src.agents.tools.notification import notification_send
from src.agents.tools.option_chain import option_chain_read
from src.agents.tools.portfolio_status import portfolio_status_read
from src.agents.tools.sandbox_dryrun import sandbox_dry_run
from src.gateway.state import get_state

SKILLS: dict[str, SkillFn] = {
    "market-data-read": market_data_read,
    "option-chain-read": option_chain_read,
    "portfolio-status-read": portfolio_status_read,
    "code-format-lint": code_format_lint,
    "sandbox-dry-run": sandbox_dry_run,
    "notification-send": notification_send,
}

__all__ = [
    "SKILLS",
    "SkillNotFoundError",
    "SkillNotGrantedError",
    "execute_skill",
    "granted_skills_for",
]


def granted_skills_for(agent_id: str) -> frozenset[str]:
    effective = get_state().get_effective_agent(agent_id)
    if effective is None:
        return frozenset()
    return frozenset(effective.skills)


async def execute_skill(agent_id: str, skill_name: str, params: dict | None = None) -> dict:
    if skill_name not in SKILLS:
        raise SkillNotFoundError(
            f"no such skill: {skill_name!r} -- only in-repo SKILLS entries can be executed"
        )
    if skill_name not in granted_skills_for(agent_id):
        raise SkillNotGrantedError(f"agent {agent_id!r} is not granted skill {skill_name!r}")
    result = SKILLS[skill_name](params or {})
    if inspect.isawaitable(result):
        return await result
    return result
