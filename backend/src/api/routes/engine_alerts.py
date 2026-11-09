"""What the trading engine has needed a person for (Phase 5): the episodes the watcher, the engine's
own events and Alertmanager recorded (src.orchestration.engine_alerts), open ones first.

Read-only for every role. Nothing here acknowledges or dismisses an alert: an episode resolves when
the condition it describes is gone, so there is no way to silence one that is still true.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import AlertmanagerStatusResponse, EngineAlertListResponse, EngineAlertResponse
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.engine_alert_event import EngineAlertEvent
from src.models.user import User
from src.notifications.alertmanager_token import read_token

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/engine-alerts", tags=["engine-alerts"])

register_policy("GET", "/api/v1/engine-alerts", roles=list(Role))
register_policy("GET", "/api/v1/engine-alerts/alertmanager", roles=list(Role))


async def get_alertmanager_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """A dependency so tests can hand in a client on an `httpx.MockTransport`."""
    async with httpx.AsyncClient(timeout=3.0) as client:
        yield client


def _response(row: EngineAlertEvent) -> EngineAlertResponse:
    return EngineAlertResponse(
        id=row.id,
        key=row.key,
        source=row.source,  # type: ignore[arg-type]
        kind=row.kind,
        severity=row.severity,  # type: ignore[arg-type]
        title=row.title,
        detail=row.detail,
        subscription_id=row.subscription_id,
        opened_at=row.opened_at,
        last_seen_at=row.last_seen_at,
        resolved_at=row.resolved_at,
        notified_at=row.notified_at,
    )


@router.get("")
async def list_engine_alerts_endpoint(
    state: Literal["open", "resolved", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> EngineAlertListResponse:
    """The newest `limit` episodes, the ones still happening first, then by when they began."""
    query = select(EngineAlertEvent)
    if state == "open":
        query = query.where(EngineAlertEvent.resolved_at.is_(None))
    elif state == "resolved":
        query = query.where(EngineAlertEvent.resolved_at.is_not(None))
    rows = (
        (
            await db.execute(
                query.order_by(
                    EngineAlertEvent.resolved_at.is_not(None),  # open (false) before resolved
                    EngineAlertEvent.opened_at.desc(),
                ).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {
        severity: count
        for severity, count in (
            await db.execute(
                select(EngineAlertEvent.severity, func.count())
                .where(EngineAlertEvent.resolved_at.is_(None))
                .group_by(EngineAlertEvent.severity)
            )
        ).all()
    }
    return EngineAlertListResponse(
        open_count=sum(counts.values()),
        critical_open_count=counts.get("critical", 0),
        alerts=[_response(row) for row in rows],
    )


@router.get("/alertmanager")
async def alertmanager_status_endpoint(
    db: AsyncSession = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_alertmanager_http_client),
    _current_user: User = Depends(require_role),
) -> AlertmanagerStatusResponse:
    """Whether Prometheus's alerts can reach a person: is Alertmanager up, does the webhook token
    exist, and when did an alert last come through it."""
    settings = get_settings()
    token_ready = read_token(settings.alertmanager_token_path) is not None
    last = (
        await db.execute(
            select(func.max(EngineAlertEvent.last_seen_at)).where(
                EngineAlertEvent.source == "alertmanager"
            )
        )
    ).scalar_one()
    if not settings.alertmanager_url:
        return AlertmanagerStatusResponse(
            configured=False,
            reachable=None,
            detail="This deployment has no Alertmanager (ALERTMANAGER_URL is not set).",
            token_ready=token_ready,
            last_alert_received_at=last,
        )
    try:
        response = await client.get(f"{settings.alertmanager_url.rstrip('/')}/-/healthy")
    except httpx.HTTPError as exc:
        return AlertmanagerStatusResponse(
            configured=True,
            reachable=False,
            detail=f"Could not reach Alertmanager: {type(exc).__name__}",
            token_ready=token_ready,
            last_alert_received_at=last,
        )
    reachable = response.status_code == 200
    return AlertmanagerStatusResponse(
        configured=True,
        reachable=reachable,
        detail="Alertmanager is up."
        if reachable
        else f"Alertmanager answered {response.status_code}.",
        token_ready=token_ready,
        last_alert_received_at=last,
    )
