"""System Vitals API (Phase 16 audit follow-up E): a real, honest JSON
summary of two of `src.observability.metrics`'s five Prometheus metrics --
LLM token usage and broker order-dispatch latency -- for the Overview
"System Vitals" panel, which previously could only say "Prometheus only"
and send an operator to Grafana for numbers this app already tracks.

Unlike `/metrics` (Prometheus's own unauthenticated scrape convention),
this is an ordinary authenticated read endpoint, same RBAC posture as
every other read surface in this codebase -- it is not a scrape target,
it is a frontend data source.
"""

from fastapi import APIRouter, Depends

from src.api.schemas import SystemVitalsResponse
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.observability.metrics import build_vitals_summary

router = APIRouter(prefix="/observability", tags=["observability"])

register_policy("GET", "/api/v1/observability/vitals", roles=list(Role))


@router.get("/vitals")
async def get_system_vitals_endpoint(
    _current_user: User = Depends(require_role),
) -> SystemVitalsResponse:
    return SystemVitalsResponse(**build_vitals_summary())
