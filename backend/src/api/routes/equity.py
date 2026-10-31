"""Equity history (Phase 3): what each mode's account was worth over time, as the drawdown monitor
measured it. Read-only for every role; the readings are written by the monitor
(src.orchestration.drawdown_monitor), never by a request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import EquityHistoryResponse, EquitySnapshotResponse
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.kill_switch import DEFAULT_MAX_DRAWDOWN_PCT, KillSwitchMode
from src.models.equity_snapshot import EquitySnapshot
from src.models.user import User
from src.orchestration.risk_limit_catalog import MAX_DRAWDOWN_LIMIT
from src.orchestration.risk_limits import get_effective_risk_limit

router = APIRouter(prefix="/equity", tags=["equity"])

register_policy("GET", "/api/v1/equity/{mode}", roles=list(Role))


@router.get("/{mode}")
async def equity_history_endpoint(
    mode: KillSwitchMode,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> EquityHistoryResponse:
    """The newest `limit` readings for `mode`, oldest first, with the drawdown limit in force (the
    chart draws it as a line). A reading that could not be measured has `equity: null`."""
    rows = (
        (
            await db.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.mode == mode)
                .order_by(EquitySnapshot.taken_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    threshold = await get_effective_risk_limit(db, MAX_DRAWDOWN_LIMIT, DEFAULT_MAX_DRAWDOWN_PCT)
    return EquityHistoryResponse(
        mode=mode,
        snapshots=[
            EquitySnapshotResponse(
                taken_at=row.taken_at,
                equity=row.equity,
                capital_base=row.capital_base,
                realized_net=row.realized_net,
                unrealized=row.unrealized,
                open_positions=row.open_positions,
                unpriced_positions=row.unpriced_positions,
                price_source=row.price_source,
                peak_equity=row.peak_equity,
                drawdown_pct=row.drawdown_pct,
                note=row.note,
            )
            for row in reversed(rows)
        ],
        threshold_pct=threshold,
    )
