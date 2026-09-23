"""System Vitals API (Phase 17 real-world testing pass): `GET
/api/v1/system/vitals`, replacing Phase 16 Follow-up E's
Prometheus-Counter-derived `/api/v1/observability/vitals` (since removed)
with a design where each figure is read the way its own semantics
actually require -- see `src.observability.vitals`'s module docstring for
the full reasoning behind the host/llm/latency split.

Same authenticated-read RBAC posture as every other read surface in this
codebase (not `/metrics`'s unauthenticated Prometheus scrape convention).
"""

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from src.agents.llm_router import active_llm_provider, llm_provider_health_vitals
from src.api.schemas import SystemVitalsResponse
from src.core.config import get_settings
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.gateway.schema import LlmProvider
from src.models.user import User
from src.observability.vitals import build_system_vitals

router = APIRouter(prefix="/system", tags=["system"])

register_policy("GET", "/api/v1/system/vitals", roles=list(Role))


@router.get("/vitals")
async def system_vitals_endpoint(
    _current_user: User = Depends(require_role),
    redis: Redis = Depends(get_redis),
) -> SystemVitalsResponse:
    settings = get_settings()
    payload = await build_system_vitals(
        redis=redis,
        token_usage_providers=[p.value for p in LlmProvider],
        prometheus_base_url=settings.prometheus_url,
        latency_budget_ms=settings.order_dispatch_latency_budget_ms,
        active_provider=active_llm_provider(),
        provider_health=llm_provider_health_vitals(),
    )
    return SystemVitalsResponse(**payload)
