"""Agent roster + LangGraph pipeline API routes (Build Spec §7.1-§7.2).
Phase 3 acceptance: "run an objective through the full LangGraph pipeline
... disable an agent and confirm its step is skipped."
"""

from fastapi import APIRouter, Depends

from src.agents.graph import run_pipeline
from src.agents.roster import capabilities_for
from src.api.schemas import AgentSummaryResponse, RunPipelineRequest, RunPipelineResponse
from src.core.rbac import Role, register_policy, require_role
from src.gateway.roster import ROSTER
from src.gateway.state import get_state
from src.models.user import User

router = APIRouter(prefix="/agents", tags=["agents"])

register_policy("GET", "/api/v1/agents", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/agents/pipeline/run",
    roles=[Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER],
)


@router.get("")
async def list_agents_endpoint(
    _current_user: User = Depends(require_role),
) -> list[AgentSummaryResponse]:
    effective_by_id = get_state().get_all_effective_agents()
    result = []
    for agent in ROSTER:
        effective = effective_by_id.get(agent.agent_id)
        result.append(
            AgentSummaryResponse(
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                department=agent.department.value,
                can_disable=agent.can_disable,
                enabled=effective.enabled if effective else True,
                capabilities=sorted(capabilities_for(agent.agent_id)),
                skills=sorted(effective.skills) if effective else [],
                heartbeat_enabled=effective.heartbeat_enabled if effective else False,
            )
        )
    return result


@router.post("/pipeline/run")
async def run_pipeline_endpoint(
    body: RunPipelineRequest,
    _current_user: User = Depends(require_role),
) -> RunPipelineResponse:
    effective_by_id = get_state().get_all_effective_agents()
    enabled_agents = (
        frozenset(agent_id for agent_id, eff in effective_by_id.items() if eff.enabled) or None
    )
    state = await run_pipeline(body.objective, enabled_agents=enabled_agents)
    return RunPipelineResponse(
        objective=state.objective,
        node_log=state.node_log,
        deployment_result=state.deployment_result,
        evaluation_verdict=state.evaluation_verdict,
        rejection_count=state.rejection_count,
    )
