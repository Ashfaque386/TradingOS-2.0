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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.graph import PipelineCancelledError, run_pipeline
from src.agents.narration import HeartbeatFact, NarrationFacts, TaskFact, narrate
from src.agents.roster import NO_DATA_SOURCE_AGENTS, capabilities_for
from src.agents.skill_catalog import build_catalog
from src.agents.tools.registry import SKILLS
from src.api.gateway_http import api_source, config_version_from_if_match, gateway_edit_error
from src.api.routes.gateway import ApplyResultResponse
from src.api.routes.memory import memory_service_dependency
from src.api.schemas import (
    AgentActivityResponse,
    AgentAnalyticsSummaryRow,
    AgentAnalyticsTrendPoint,
    AgentHeartbeatEntryResponse,
    AgentNarrationResponse,
    AgentPipelineEventResponse,
    AgentSummaryResponse,
    AgentTaskEntryResponse,
    CancelPipelineRunResponse,
    CreatePromptVersionRequest,
    NarrationLineResponse,
    PromptVersionResponse,
    RunPipelineRequest,
    RunPipelineResponse,
    SetAgentIdentityRequest,
    SkillCatalogEntryResponse,
    UpdateAgentSettingsRequest,
    UpdateAgentSettingsResponse,
)
from src.core.config import get_settings
from src.core.db import get_db, get_session_factory
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.gateway.edits import AgentSettingsPatch, EditRefused
from src.gateway.loader import ConfigLoadError
from src.gateway.roster import ROSTER, ROSTER_BY_ID
from src.gateway.service import (
    ServiceError,
    StaleConfigError,
    set_identity,
    update_agent_settings,
)
from src.gateway.state import get_state
from src.memory.service import MemoryService, get_pipeline_memory
from src.models.agent_pipeline_event import AgentPipelineEvent
from src.models.heartbeat_log import HeartbeatLog
from src.models.prompt_version import PromptVersion
from src.models.task import Task, TaskStatus
from src.models.user import User
from src.orchestration.pipeline_events import (
    PipelineEventRecorder,
    clear_cancel,
    list_pipeline_events,
    pipeline_run_state,
    request_cancel,
)
from src.orchestration.prompt_versions import (
    NoSuchPromptVersionError,
    PromptVersionAlreadyActiveError,
    activate_prompt_version,
    create_prompt_version,
    get_active_prompts,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/agents", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/agents/pipeline/run",
    roles=[Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER],
)
register_policy("GET", "/api/v1/agents/pipeline/events", roles=list(Role))
# Stopping a run is held to the same roles as starting one.
register_policy(
    "POST",
    "/api/v1/agents/pipeline/runs/{run_id}/cancel",
    roles=[Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER],
)
register_policy("PUT", "/api/v1/agents/{agent_id}/identity", roles=_WRITE_ROLES)
register_policy("PATCH", "/api/v1/agents/{agent_id}", roles=_WRITE_ROLES)
register_policy("GET", "/api/v1/agents/skills", roles=list(Role))
register_policy("GET", "/api/v1/agents/{agent_id}/narration", roles=list(Role))
register_policy("GET", "/api/v1/agents/{agent_id}/activity", roles=list(Role))
register_policy("GET", "/api/v1/agents/analytics/summary", roles=list(Role))
register_policy("GET", "/api/v1/agents/analytics/trend", roles=list(Role))
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
    version_id = get_state().get_version_id()
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
                has_data_source=agent.agent_id not in NO_DATA_SOURCE_AGENTS,
                config_version_id=version_id,
            )
        )
    return result


@router.get("/skills")
async def skill_catalog_endpoint(
    _current_user: User = Depends(require_role),
) -> list[SkillCatalogEntryResponse]:
    """The skills the app ships with, whether each does anything yet, and which agents hold it. The
    fixed set an agent can be granted: there is no way to add one, so a grant is a choice from this
    list, not a typed id."""
    return [
        SkillCatalogEntryResponse(
            skill_id=entry.info.skill_id,
            summary=entry.info.summary,
            effect=entry.info.effect,
            working=entry.info.working,
            granted_to=list(entry.granted_to),
        )
        for entry in build_catalog(get_state().get_all_effective_agents())
    ]


@router.put("/{agent_id}/identity")
async def set_agent_identity_endpoint(
    agent_id: str,
    body: SetAgentIdentityRequest,
    current_user: User = Depends(require_role),
    version: int = Depends(config_version_from_if_match),
    db: AsyncSession = Depends(get_db),
    config_path: Path = Depends(_config_path),
) -> ApplyResultResponse:
    """Saves an agent's identity on the config version the editor read (a config that has moved on
    is a 409). The audit trail names the person, `api:<email>`; it used to say `cli`."""
    source, actor = api_source(current_user.email)
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
            source=source,
            actor=actor,
            expected_version=version,
        )
    except (ServiceError, StaleConfigError, ConfigLoadError) as exc:
        raise gateway_edit_error(exc) from exc
    return ApplyResultResponse(
        status=result.status.value, version_id=result.version_id, errors=result.errors
    )


@router.patch("/{agent_id}")
async def update_agent_settings_endpoint(
    agent_id: str,
    body: UpdateAgentSettingsRequest,
    current_user: User = Depends(require_role),
    version: int = Depends(config_version_from_if_match),
    db: AsyncSession = Depends(get_db),
    config_path: Path = Depends(_config_path),
) -> UpdateAgentSettingsResponse:
    """Changes one agent's settings (switched on or off, heartbeat, skills) and nothing else in the
    config. The edit is made on the server, on the config version the editor read: the console used
    to fetch the whole config, change a piece and write the whole config back, so two editors
    overwrote each other. A skill must be one the app ships with; a request that changes nothing is
    refused, so it does not write a version and an audit row for something that did not happen."""
    patch = AgentSettingsPatch(
        enabled=body.enabled,
        heartbeat_enabled=body.heartbeat_enabled,
        heartbeat_interval_minutes=body.heartbeat_interval_minutes,
        skills=tuple(body.skills) if body.skills is not None else None,
    )
    source, actor = api_source(current_user.email)
    try:
        result, changed = await update_agent_settings(
            db,
            config_path,
            agent_id,
            patch,
            known_skills=SKILLS.keys(),
            source=source,
            actor=actor,
            expected_version=version,
        )
    except (EditRefused, StaleConfigError, ConfigLoadError) as exc:
        raise gateway_edit_error(exc) from exc
    if result.status.value != "active":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(result.errors))
    return UpdateAgentSettingsResponse(
        status=result.status.value,
        version_id=result.version_id,
        errors=result.errors,
        changed=changed,
    )


async def _recent_activity(
    db: AsyncSession, agent_id: str
) -> tuple[list[HeartbeatLog], list[Task]]:
    """The agent's last 20 heartbeat self-checks and last 20 tasks claimed under its capabilities,
    newest first: what the activity tab lists and what the narration is told."""
    heartbeat_rows = list(
        (
            await db.execute(
                select(HeartbeatLog)
                .where(HeartbeatLog.agent_id == agent_id)
                .order_by(HeartbeatLog.checked_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    capabilities = capabilities_for(agent_id)
    task_rows: list[Task] = []
    if capabilities:
        task_rows = list(
            (
                await db.execute(
                    select(Task)
                    .where(Task.capability.in_(capabilities))
                    .order_by(Task.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    return heartbeat_rows, task_rows


@router.get("/{agent_id}/narration")
async def agent_narration_endpoint(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> AgentNarrationResponse:
    """What this agent is doing, in plain sentences, from its settings, heartbeat self-checks and
    tasks, with what is missing said as plainly as what is there (see `src.agents.narration`)."""
    _require_roster_agent_or_404(agent_id)
    roster_agent = ROSTER_BY_ID[agent_id]
    effective = get_state().get_effective_agent(agent_id)
    heartbeat_rows, task_rows = await _recent_activity(db, agent_id)
    now = datetime.now(UTC)
    lines = narrate(
        NarrationFacts(
            display_name=effective.identity_name if effective else roster_agent.display_name,
            enabled=effective.enabled if effective else True,
            can_disable=roster_agent.can_disable,
            heartbeat_enabled=effective.heartbeat_enabled if effective else False,
            skills=tuple(sorted(effective.skills)) if effective else (),
            has_data_source=agent_id not in NO_DATA_SOURCE_AGENTS,
            capabilities=tuple(sorted(capabilities_for(agent_id))),
            heartbeats=[HeartbeatFact(row.status.value, row.checked_at) for row in heartbeat_rows],
            tasks=[
                TaskFact(row.name, row.status.value, row.last_error, row.created_at)
                for row in task_rows
            ],
            now=now,
        )
    )
    return AgentNarrationResponse(
        agent_id=agent_id,
        as_of=now.isoformat(),
        lines=[NarrationLineResponse(kind=line.kind, text=line.text) for line in lines],
    )


@router.get("/{agent_id}/activity")
async def agent_activity_endpoint(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> AgentActivityResponse:
    """Phase 19 (docs/phase19-audit.md Part 2.2/3.2): every real, per-agent
    activity signal this codebase has -- heartbeat self-checks
    (src.agents.heartbeat, empty when heartbeat is disabled for this
    agent) and orchestration tasks claimed under one of this agent's
    registered capabilities (Task has no agent_id column, so capability
    is the real join key -- src.agents.roster.capabilities_for)."""
    _require_roster_agent_or_404(agent_id)
    heartbeat_rows, task_rows = await _recent_activity(db, agent_id)

    return AgentActivityResponse(
        agent_id=agent_id,
        heartbeats=[
            AgentHeartbeatEntryResponse(
                status=row.status.value,
                details=row.details,
                checked_at=row.checked_at.isoformat(),
            )
            for row in heartbeat_rows
        ],
        tasks=[
            AgentTaskEntryResponse(
                id=row.id,
                run_id=row.run_id,
                name=row.name,
                status=row.status.value,
                last_error=row.last_error,
                created_at=row.created_at.isoformat(),
                completed_at=row.completed_at.isoformat() if row.completed_at else None,
            )
            for row in task_rows
        ],
    )


_TERMINAL_TASK_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.FAILED)


@router.get("/analytics/summary")
async def agent_analytics_summary_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[AgentAnalyticsSummaryRow]:
    """Phase 22 (docs/phase20-old-vs-new-comparison.md item 21): real task
    counts per roster agent, the same capability join `/{agent_id}/activity`
    already uses -- one row per agent, in real roster order, an honest
    all-zero/None row (never omitted) for an agent that has never claimed
    a task rather than a shorter, silently-incomplete list."""
    rows: list[AgentAnalyticsSummaryRow] = []
    for agent in ROSTER:
        capabilities = capabilities_for(agent.agent_id)
        if not capabilities:
            rows.append(
                AgentAnalyticsSummaryRow(
                    agent_id=agent.agent_id,
                    display_name=agent.display_name,
                    tasks_total=0,
                    tasks_succeeded=0,
                    tasks_failed=0,
                    success_rate=None,
                    avg_duration_seconds=None,
                )
            )
            continue

        counts_stmt = (
            select(Task.status, func.count())
            .where(Task.capability.in_(capabilities))
            .group_by(Task.status)
        )
        counts: dict[TaskStatus, int] = {
            status: count for status, count in (await db.execute(counts_stmt)).all()
        }
        succeeded = counts.get(TaskStatus.SUCCEEDED, 0)
        failed = counts.get(TaskStatus.FAILED, 0)
        total = sum(counts.values())
        terminal = succeeded + failed

        duration_stmt = select(
            func.avg(func.extract("epoch", Task.completed_at - Task.created_at))
        ).where(
            Task.capability.in_(capabilities),
            Task.status.in_(_TERMINAL_TASK_STATUSES),
            Task.completed_at.is_not(None),
        )
        avg_duration = (await db.execute(duration_stmt)).scalar_one_or_none()

        rows.append(
            AgentAnalyticsSummaryRow(
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                tasks_total=total,
                tasks_succeeded=succeeded,
                tasks_failed=failed,
                success_rate=(succeeded / terminal) if terminal else None,
                avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
            )
        )
    return rows


@router.get("/analytics/trend")
async def agent_analytics_trend_endpoint(
    days: int = 14,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[AgentAnalyticsTrendPoint]:
    """Real, system-wide (not per-agent -- see AgentAnalyticsTrendPoint's
    own docstring) daily task-outcome counts over the trailing `days`
    days, bucketed on `completed_at` (a task only has an outcome once it's
    terminal) -- a day with zero tasks is a real zero, not an omitted
    point, so the chart's x-axis is never silently gappy."""
    if days < 1 or days > 90:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "days must be between 1 and 90")

    since = datetime.now(UTC) - timedelta(days=days)
    bucket = func.date(Task.completed_at)
    stmt = (
        select(bucket, Task.status, func.count())
        .where(
            Task.completed_at.is_not(None),
            Task.completed_at >= since,
            Task.status.in_(_TERMINAL_TASK_STATUSES),
        )
        .group_by(bucket, Task.status)
        .order_by(bucket)
    )
    rows = (await db.execute(stmt)).all()

    by_date: dict[str, dict[str, int]] = {}
    for day, task_status, count in rows:
        day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        by_date.setdefault(day_key, {"succeeded": 0, "failed": 0})
        by_date[day_key]["succeeded" if task_status == TaskStatus.SUCCEEDED else "failed"] = count

    return [
        AgentAnalyticsTrendPoint(
            date=day_key,
            tasks_succeeded=counts["succeeded"],
            tasks_failed=counts["failed"],
        )
        for day_key, counts in sorted(by_date.items())
    ]


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
    current_user: User = Depends(require_role),
) -> PromptVersionResponse:
    _require_roster_agent_or_404(agent_id)
    try:
        version = await activate_prompt_version(
            db, agent_id=agent_id, version_id=version_id, actor=current_user.email
        )
    except NoSuchPromptVersionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PromptVersionAlreadyActiveError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _prompt_version_response(version)


def _pipeline_event_response(event: AgentPipelineEvent) -> AgentPipelineEventResponse:
    return AgentPipelineEventResponse(
        run_id=str(event.run_id),
        sequence=event.sequence,
        event_type=event.event_type,
        node=event.node,
        agent_id=event.agent_id,
        payload=event.payload,
        created_at=event.created_at.isoformat(),
    )


@router.get("/pipeline/events")
async def list_pipeline_events_endpoint(
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[AgentPipelineEventResponse]:
    """Phase 19 Finding #1's history half: replay one pipeline run's steps
    (`run_id`, in order) or, with no `run_id`, the most recent `limit` steps
    across runs -- what the Mission Control feed backfills from on open. The
    live half is `/ws/agent-pipeline-events`."""
    events = await list_pipeline_events(db, run_id=run_id, limit=limit)
    return [_pipeline_event_response(e) for e in events]


@router.post("/pipeline/run")
async def run_pipeline_endpoint(
    body: RunPipelineRequest,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    redis: Redis = Depends(get_redis),
    memory_service: MemoryService = Depends(memory_service_dependency),
    _current_user: User = Depends(require_role),
) -> RunPipelineResponse:
    effective_by_id = get_state().get_all_effective_agents()
    enabled_agents = (
        frozenset(agent_id for agent_id, eff in effective_by_id.items() if eff.enabled) or None
    )
    # Phase 19 (docs/phase19-audit.md Part 2.2): resolves each LLM-backed
    # node's real ACTIVE prompt version, if one has been activated --
    # the only place in this codebase where run_pipeline's DB-independence
    # is bridged to the real prompt_versions table, since run_pipeline
    # itself deliberately stays DB-independent (used by e.g. the Screener
    # Agent's own strategy-creation path, which has no reason to touch
    # prompt_versions at all).
    active_prompts = await get_active_prompts(
        db, ["ceo-agent", "strategy-generator", "python-code-generator"]
    )
    # Phase 19 Finding #1: every step of this run is recorded and published
    # live under this id (see src.orchestration.pipeline_events).
    run_id = uuid.uuid4()
    recorder = PipelineEventRecorder(session_factory, redis, run_id)
    cancelled = False
    try:
        # The heartbeat is what tells a later reader this run is alive rather
        # than abandoned by a process that died mid-run.
        async with recorder.heartbeat():
            state = await run_pipeline(
                body.objective,
                enabled_agents=enabled_agents,
                active_prompts=active_prompts,
                run_id=str(run_id),
                event_sink=recorder,
                memory=get_pipeline_memory(memory_service),
                cancel_signal=recorder,
            )
    except PipelineCancelledError as exc:
        # Not an error: the operator asked for this. Report what finished
        # before it stopped.
        state = exc.state
        cancelled = True
    finally:
        try:
            await clear_cancel(redis, run_id)
        except Exception:  # noqa: BLE001 - housekeeping; the flag also expires on its own
            logger.warning("agents.pipeline_cancel_flag_not_cleared", run_id=str(run_id))
    return RunPipelineResponse(
        objective=state.objective,
        node_log=state.node_log,
        deployment_result=state.deployment_result,
        evaluation_verdict=state.evaluation_verdict,
        rejection_count=state.rejection_count,
        run_id=str(run_id),
        cancelled=cancelled,
    )


@router.post("/pipeline/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_pipeline_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> CancelPipelineRunResponse:
    """Ask a running pipeline to stop. The run executes inside the request that
    started it, so this only sets a flag; the run notices it at its next step
    boundary (a step already in progress -- an LLM call -- finishes first) and
    ends with a `pipeline.cancelled` event. 202, because nothing has stopped
    yet. 404 for an id no event was ever recorded under; 409 for a run that
    already ended, or whose process died (nothing left to stop -- and no audit
    row for a cancel that did nothing, since the audit trail records only 2xx
    responses)."""
    state = await pipeline_run_state(db, run_id, redis)
    if state == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pipeline run")
    if state == "finished":
        raise HTTPException(status.HTTP_409_CONFLICT, "This pipeline run has already finished")
    if state == "abandoned":
        # Its process died: there is nothing running to read the flag, so
        # accepting the cancel (a 202, an audit row) would be a lie.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This pipeline run is no longer running -- the process executing it ended",
        )
    try:
        await request_cancel(redis, run_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 503, not swallowed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not record the cancel request (Redis is unavailable)",
        ) from exc
    return CancelPipelineRunResponse(run_id=str(run_id))
