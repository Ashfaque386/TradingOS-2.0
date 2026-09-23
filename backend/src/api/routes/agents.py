"""Agent roster + LangGraph pipeline API routes (Build Spec §7.1-§7.2).
Phase 3 acceptance: "run an objective through the full LangGraph pipeline
... disable an agent and confirm its step is skipped."

Identity editing and prompt-version history (added in a later pass):
`src.gateway.service.set_identity` and `src.orchestration.prompt_versions`
have existed, fully unit-tested, since Phase 3 -- only exercised through
`tradingos-cli` before now, with zero HTTP routes and zero frontend
wiring (`app/agent-fleet/page.tsx`'s Identity tab explicitly said so:
"Identity fields ... have no editable backend surface yet"). These routes
are a thin HTTP front for those exact same functions, the same posture
`src.api.routes.gateway` already established for the rest of the Gateway
config surface -- a write here goes through the identical
validate-write-apply pipeline (atomic file write, schema validation, DB
sync, audit row) `tradingos-cli` already goes through, never a new
mutation path.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.graph import run_pipeline
from src.agents.roster import capabilities_for
from src.api.routes.gateway import ApplyResultResponse
from src.api.schemas import (
    AgentSummaryResponse,
    CreatePromptVersionRequest,
    PromptVersionResponse,
    RunPipelineRequest,
    RunPipelineResponse,
    SetAgentIdentityRequest,
)
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.gateway.roster import ROSTER, ROSTER_BY_ID
from src.gateway.service import ServiceError, set_identity
from src.gateway.state import get_state
from src.models.prompt_version import PromptVersion
from src.models.user import User
from src.orchestration.prompt_versions import (
    NoSuchPromptVersionError,
    activate_prompt_version,
    create_prompt_version,
)

router = APIRouter(prefix="/agents", tags=["agents"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/agents", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/agents/pipeline/run",
    roles=[Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER],
)
register_policy("PUT", "/api/v1/agents/{agent_id}/identity", roles=_WRITE_ROLES)
register_policy("GET", "/api/v1/agents/{agent_id}/prompt-versions", roles=list(Role))
register_policy("POST", "/api/v1/agents/{agent_id}/prompt-versions", roles=_WRITE_ROLES)
register_policy(
    "POST", "/api/v1/agents/{agent_id}/prompt-versions/{version_id}/activate", roles=_WRITE_ROLES
)


def _config_path() -> Path:
    return Path(get_settings().agent_gateway_config_path)


def _require_roster_agent_or_404(agent_id: str) -> None:
    if agent_id not in ROSTER_BY_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown agent id: {agent_id!r}")


def _prompt_version_response(row: PromptVersion) -> PromptVersionResponse:
    return PromptVersionResponse(
        id=row.id,
        agent_id=row.agent_id,
        version_number=row.version_number,
        content=row.content,
        status=row.status.value,
        diff_from_previous=row.diff_from_previous,
        created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        activated_at=row.activated_at.isoformat() if row.activated_at else None,
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
                identity_name=effective.identity_name if effective else agent.display_name,
                emoji=effective.emoji if effective else None,
                avatar=effective.avatar if effective else None,
                theme=effective.theme if effective else None,
                voice=effective.voice if effective else None,
            )
        )
    return result


@router.put("/{agent_id}/identity")
async def set_agent_identity_endpoint(
    agent_id: str,
    body: SetAgentIdentityRequest,
    db: AsyncSession = Depends(get_db),
    config_path: Path = Depends(_config_path),
    current_user: User = Depends(require_role),
) -> ApplyResultResponse:
    try:
        result = await set_identity(
            db,
            config_path,
            agent_id,
            name=body.name,
            emoji=body.emoji,
            avatar=body.avatar,
            theme=body.theme,
            voice=body.voice,
        )
    except ServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ApplyResultResponse(
        status=result.status.value, version_id=result.version_id, errors=result.errors
    )


@router.get("/{agent_id}/prompt-versions")
async def list_prompt_versions_endpoint(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[PromptVersionResponse]:
    _require_roster_agent_or_404(agent_id)
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.agent_id == agent_id)
        .order_by(PromptVersion.version_number.desc())
    )
    return [_prompt_version_response(row) for row in result.scalars().all()]


@router.post("/{agent_id}/prompt-versions", status_code=status.HTTP_201_CREATED)
async def create_prompt_version_endpoint(
    agent_id: str,
    body: CreatePromptVersionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> PromptVersionResponse:
    _require_roster_agent_or_404(agent_id)
    version = await create_prompt_version(
        db, agent_id=agent_id, content=body.content, created_by=current_user.email
    )
    return _prompt_version_response(version)


@router.post("/{agent_id}/prompt-versions/{version_id}/activate")
async def activate_prompt_version_endpoint(
    agent_id: str,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> PromptVersionResponse:
    _require_roster_agent_or_404(agent_id)
    try:
        version = await activate_prompt_version(db, agent_id=agent_id, version_id=version_id)
    except NoSuchPromptVersionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _prompt_version_response(version)


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
