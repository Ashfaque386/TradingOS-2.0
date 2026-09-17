"""Agent Gateway config REST API (Build Spec §6, Phase 13 frontend
wiring): the Settings page's "Agent Gateway Config" section had no HTTP
surface at all before this phase -- only a CLI (`tradingos-cli`) and the
file-watcher hot-reload existed. This is a thin HTTP front for the exact
same `src.gateway.service`/`apply` functions the CLI already calls, not a
new mutation path: a PUT here goes through the identical
validate-write-apply pipeline (atomic file write, then
`apply_config_text`'s schema validation + DB sync + audit row) a hand
edit or `tradingos-cli config apply` already goes through, so it can
never bypass validation or skip the audit trail.

`riskThresholdRefs` (`infra.riskThresholdRefs`) is deliberately read-only
here, matching `TradingOSConfig`'s own module docstring: those two values
are pointers into the real dual-control risk-limits flow
(`src/api/routes/risk_limits.py`), not settable directly through this
config -- a `PUT` payload's `infra.riskThresholdRefs` is accepted (the
schema requires the field to be present) but changing it here has no
special effect beyond being part of the applied config text; the actual
enforced threshold always comes from `src.orchestration.risk_limits`.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.gateway.apply import apply_config_text
from src.gateway.loader import (
    ConfigLoadError,
    load_and_validate,
    parse_config_text,
    read_config_text,
    validate_config_dict,
)
from src.gateway.service import ServiceError, _atomic_write, rollback_config
from src.gateway.state import get_state
from src.models.agent_config_version import AgentConfigVersion
from src.models.user import User

router = APIRouter(prefix="/gateway", tags=["gateway"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/gateway/config", roles=list(Role))
register_policy("PUT", "/api/v1/gateway/config", roles=_WRITE_ROLES)
register_policy("POST", "/api/v1/gateway/config/validate", roles=_WRITE_ROLES)
register_policy("GET", "/api/v1/gateway/config/versions", roles=list(Role))
register_policy("POST", "/api/v1/gateway/config/versions/{version_id}/rollback", roles=_WRITE_ROLES)


def _config_path() -> Path:
    return Path(get_settings().agent_gateway_config_path)


class GatewayConfigResponse(BaseModel):
    raw_text: str
    parsed: dict
    version_id: int | None


class WriteGatewayConfigRequest(BaseModel):
    raw_text: str


class ValidateGatewayConfigRequest(BaseModel):
    raw_text: str


class ValidateGatewayConfigResponse(BaseModel):
    ok: bool
    errors: list[str]


class ApplyResultResponse(BaseModel):
    status: str
    version_id: int
    errors: list[str]


class ConfigVersionSummaryResponse(BaseModel):
    id: int
    status: str
    source: str
    created_at: str
    validation_errors: list | None


@router.get("/config")
async def get_gateway_config_endpoint(
    _current_user: User = Depends(require_role),
) -> GatewayConfigResponse:
    path = _config_path()
    try:
        raw_text = read_config_text(path)
        config = load_and_validate(path)
    except ConfigLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"config on disk is currently invalid: {'; '.join(exc.errors)}",
        ) from exc
    return GatewayConfigResponse(
        raw_text=raw_text,
        parsed=config.model_dump(mode="json", by_alias=True),
        version_id=get_state().get_version_id(),
    )


@router.put("/config")
async def put_gateway_config_endpoint(
    body: WriteGatewayConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> ApplyResultResponse:
    _atomic_write(_config_path(), body.raw_text)
    result = await apply_config_text(db, body.raw_text, source=f"api:{current_user.email}")
    return ApplyResultResponse(
        status=result.status.value, version_id=result.version_id, errors=result.errors
    )


@router.post("/config/validate")
async def validate_gateway_config_endpoint(
    body: ValidateGatewayConfigRequest,
    _current_user: User = Depends(require_role),
) -> ValidateGatewayConfigResponse:
    try:
        data = parse_config_text(body.raw_text)
        validate_config_dict(data, raw_text=body.raw_text)
    except ConfigLoadError as exc:
        return ValidateGatewayConfigResponse(ok=False, errors=exc.errors)
    return ValidateGatewayConfigResponse(ok=True, errors=[])


@router.get("/config/versions")
async def list_gateway_config_versions_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[ConfigVersionSummaryResponse]:
    rows = (
        (
            await db.execute(
                select(AgentConfigVersion).order_by(AgentConfigVersion.id.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [
        ConfigVersionSummaryResponse(
            id=row.id,
            status=row.status.value,
            source=row.source,
            created_at=row.created_at.isoformat(),
            validation_errors=row.validation_errors,
        )
        for row in rows
    ]


@router.post("/config/versions/{version_id}/rollback")
async def rollback_gateway_config_endpoint(
    version_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> ApplyResultResponse:
    try:
        result = await rollback_config(db, _config_path(), version_id)
    except ServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ApplyResultResponse(
        status=result.status.value, version_id=result.version_id, errors=result.errors
    )
