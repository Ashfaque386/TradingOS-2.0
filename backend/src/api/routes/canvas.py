"""The Live Canvas (Phase 22, docs/phase20-old-vs-new-comparison.md item
19): one composed read over data that's all real and already persisted
elsewhere in this codebase (StrategyVersion.code from the real strategy
pipeline, BacktestRun from the real vectorized backtest engine, AuditLog
from the real audit trail every mutation already writes to) -- nothing
new is computed or fabricated here, this just answers "what's the newest
real artifact of each kind, across every strategy/run, right now" so the
frontend doesn't need three separate polling loops with client-side
"which one is newest" logic.

Risk data is intentionally not duplicated here, matching the old app's
own Live Canvas design (`docs/phase20-legacy-audit.md`'s Agents,
Orchestration & Memory section) -- this codebase already has real
endpoints for that (`/risk/*`, `/kill-switch/*`).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.audit_log import AuditLog
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.strategy import Strategy
from src.models.strategy_version import StrategyVersion
from src.models.user import User

router = APIRouter(prefix="/canvas", tags=["canvas"])

register_policy("GET", "/api/v1/canvas/state", roles=list(Role))


class LatestStrategyCode(BaseModel):
    strategy_id: uuid.UUID
    strategy_name: str
    version_id: uuid.UUID
    version_number: int
    code: str
    created_at: datetime


class LatestBacktestResult(BaseModel):
    backtest_id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str
    symbol: str
    status: str
    metrics: dict | None
    created_at: datetime


class LatestAuditEvent(BaseModel):
    sequence: int
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    created_at: datetime


class CanvasStateResponse(BaseModel):
    latest_strategy_code: LatestStrategyCode | None
    latest_backtest_result: LatestBacktestResult | None
    latest_agent_log: LatestAuditEvent | None


@router.get("/state")
async def canvas_state_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> CanvasStateResponse:
    version_row = (
        await db.execute(
            select(StrategyVersion, Strategy.name)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .order_by(StrategyVersion.created_at.desc())
            .limit(1)
        )
    ).first()
    latest_strategy_code = (
        LatestStrategyCode(
            strategy_id=version_row[0].strategy_id,
            strategy_name=version_row[1],
            version_id=version_row[0].id,
            version_number=version_row[0].version_number,
            code=version_row[0].code,
            created_at=version_row[0].created_at,
        )
        if version_row
        else None
    )

    backtest_row = (
        await db.execute(
            select(BacktestRun, Strategy.id, Strategy.name)
            .join(StrategyVersion, StrategyVersion.id == BacktestRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(BacktestRun.status == BacktestStatus.COMPLETED)
            .order_by(BacktestRun.created_at.desc())
            .limit(1)
        )
    ).first()
    latest_backtest_result = (
        LatestBacktestResult(
            backtest_id=backtest_row[0].id,
            strategy_id=backtest_row[1],
            strategy_name=backtest_row[2],
            symbol=backtest_row[0].symbol,
            status=backtest_row[0].status,
            metrics=backtest_row[0].metrics,
            created_at=backtest_row[0].created_at,
        )
        if backtest_row
        else None
    )

    audit_row = (
        await db.execute(select(AuditLog).order_by(AuditLog.sequence.desc()).limit(1))
    ).scalar_one_or_none()
    latest_agent_log = (
        LatestAuditEvent(
            sequence=audit_row.sequence,
            actor=audit_row.actor,
            action=audit_row.action,
            entity_type=audit_row.entity_type,
            entity_id=audit_row.entity_id,
            created_at=audit_row.created_at,
        )
        if audit_row
        else None
    )

    return CanvasStateResponse(
        latest_strategy_code=latest_strategy_code,
        latest_backtest_result=latest_backtest_result,
        latest_agent_log=latest_agent_log,
    )
