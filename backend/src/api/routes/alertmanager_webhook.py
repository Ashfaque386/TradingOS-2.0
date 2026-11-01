"""Where Alertmanager posts the alerts Prometheus raises (Phase 5).

Prometheus evaluates the rules in observability/prometheus/alert_rules.yml -- a quote feed that has
gone stale, an order that blew its latency budget, the alert watcher itself having stopped -- and
hands what fires to Alertmanager, which groups them and calls this route. Each alert becomes an
engine alert episode (src.orchestration.engine_alerts): the subscribed channels hear about it once
when it starts firing and once when it resolves, however often Alertmanager repeats it in between.

Deliberately **not** RBAC-gated (no `Depends(require_role)`/`register_policy`): Alertmanager is
another container, with no user to sign in as. The route is authenticated by the bearer token the
backend created on first boot and Alertmanager reads from the same volume
(src.notifications.alertmanager_token), compared in constant time. Anything else is a 401 and is
not parsed.

One thing it cannot do: report the backend being down. A backend that is not running cannot receive
this call, so `MarketHoursBackendDown` is visible in Prometheus and Grafana but reaches no channel
through here.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import get_settings
from src.core.db import get_session_factory
from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.alertmanager_token import token_matches
from src.orchestration.engine_alerts import (
    AlertCandidate,
    announce_opened,
    announce_resolved,
    open_episode,
    open_or_refresh,
    resolve_row,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_MAX_BODY_BYTES = 512 * 1024
_MAX_ALERTS = 200
_SEVERITIES = {"critical": "critical", "warning": "warning", "info": "info"}
_KEY_MAX = 200


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _key(alert: dict[str, Any]) -> str:
    """The same alert always gets the same key: Alertmanager's own fingerprint, or a hash of the
    labels if an old version did not send one."""
    fingerprint = str(alert.get("fingerprint") or "")
    if not fingerprint:
        labels = alert.get("labels") or {}
        canonical = json.dumps(labels, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"alertmanager:{fingerprint}"[:_KEY_MAX]


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _candidate(alert: dict[str, Any]) -> AlertCandidate:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = _text(labels.get("alertname"), 120) or "Prometheus alert"
    return AlertCandidate(
        key=_key(alert),
        kind=name[:48],
        severity=_SEVERITIES.get(str(labels.get("severity", "")).lower(), "warning"),  # type: ignore[arg-type]
        title=_text(annotations.get("summary"), 300) or name,
        detail=_text(annotations.get("description"), 2000),
    )


@router.post("/alertmanager")
async def alertmanager_webhook(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    authorization: str | None = Header(default=None),
) -> Response:
    if not token_matches(_bearer(authorization), get_settings().alertmanager_token_path):
        logger.warning("webhooks.rejected", channel="alertmanager", reason="bad or missing token")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, content="invalid token")

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return Response(status_code=413, content="too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="invalid JSON")
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list) or not all(isinstance(a, dict) for a in alerts):
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="no alerts list")

    opened: list[EngineAlertEvent] = []
    resolved: list[EngineAlertEvent] = []
    ignored = 0
    async with session_factory() as db:
        now = datetime.now(UTC)
        for alert in alerts[:_MAX_ALERTS]:
            if alert.get("status") == "resolved":
                row = await open_episode(db, _key(alert))
                if row is None:
                    ignored += 1  # it was never reported (or already cleared): nothing to close
                    continue
                resolve_row(row, now=now)
                resolved.append(row)
            else:
                row, created = await open_or_refresh(db, "alertmanager", _candidate(alert), now=now)
                if created:
                    opened.append(row)
        await announce_opened(opened, now=now)
        await announce_resolved(resolved, now=now)
        await db.commit()
    logger.info(
        "alertmanager.webhook",
        received=len(alerts),
        opened=len(opened),
        resolved=len(resolved),
        ignored=ignored,
    )
    return Response(
        status_code=status.HTTP_200_OK,
        content=json.dumps(
            {"received": len(alerts), "opened": len(opened), "resolved": len(resolved)}
        ),
        media_type="application/json",
    )
