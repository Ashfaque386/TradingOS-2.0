"""Agent-to-agent access policy enforcement (Build Spec §6.4).

Default-deny: every cross-agent read of another agent's artefacts, logs,
or session state must match an explicit allow rule (from, to, scope) in
config.agentToAgentPolicy.allow, mirrored into the agent_to_agent_policy
table by src/gateway/apply.py on every successful config apply. This is
the ORM/query-layer gate — call is_allowed() from any future query code
that crosses agent boundaries, not just from API routes.

The rest of the orchestration engine (planner, task engine, run manager,
approvals, events) is built in Phase 2.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_to_agent_policy import AgentToAgentPolicy


async def is_allowed(db: AsyncSession, from_agent: str, to_agent: str, scope: str) -> bool:
    # An agent reading its own artefacts isn't a cross-agent access at all
    # ("every cross-agent read of *another* agent's artefacts" — §6.4) —
    # nothing to allow-list.
    if from_agent == to_agent:
        return True

    stmt = select(AgentToAgentPolicy.id).where(
        AgentToAgentPolicy.from_agent == from_agent,
        AgentToAgentPolicy.to_agent == to_agent,
        AgentToAgentPolicy.scope == scope,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None
