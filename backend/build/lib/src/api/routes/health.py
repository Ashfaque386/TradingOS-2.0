from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.observability.metrics import render_latest_metrics

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    """Standard Prometheus scrape endpoint (Build Spec §19) -- deliberately
    unauthenticated, matching Prometheus's own convention (a real
    deployment restricts this at the network layer, not via RBAC; see
    src.observability.metrics's module docstring for why no RBAC role is
    invented for it here).
    """
    body, content_type = render_latest_metrics()
    return Response(content=body, media_type=content_type)
