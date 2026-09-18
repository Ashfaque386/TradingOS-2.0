"""Unified Orders & Trades API (Phase 13 frontend wiring): the frontend's
Orders & Trades page wants one execution ledger spanning both paper and
live fills, but this codebase deliberately keeps paper and live execution
as two separate real pipelines with two separate model sets (Phase 7's
`PaperFill` vs Phase 9's `Order`/`Trade`) -- paper trading was never meant
to share a table with real broker-routed orders. This route is a
read-only aggregator over both, not a new execution path: it queries the
same rows `GET /paper-trading/.../fills` and `GET /live-trading/.../trades`
already expose, normalizes them into one shape, and never writes
anything.

No "latency" field exists anywhere in either model -- that KPI is a real
Prometheus histogram (`tradingos_order_dispatch_latency_seconds`, Phase
11), not a per-row database column, so it is deliberately left off this
response rather than fabricated; the frontend should read it from
`GET /metrics` if it wants the aggregate figure.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy_version import StrategyVersion
from src.models.trade import Trade
from src.models.user import User

router = APIRouter(prefix="/orders", tags=["orders"])

register_policy("GET", "/api/v1/orders", roles=list(Role))


class UnifiedExecutionResponse(BaseModel):
    id: uuid.UUID
    mode: Literal["paper", "live"]
    time: str
    symbol: str
    side: str
    quantity: int
    price: float | None
    status: str
    strategy_id: uuid.UUID | None


async def _live_executions(db: AsyncSession) -> list[UnifiedExecutionResponse]:
    rows = (
        await db.execute(
            select(Trade, Order.strategy_id)
            .join(Order, Trade.order_id == Order.id)
            .order_by(Trade.executed_at.desc())
        )
    ).all()
    return [
        UnifiedExecutionResponse(
            id=trade.id,
            mode="live",
            time=trade.executed_at.isoformat(),
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            price=trade.price,
            status=trade.status,
            strategy_id=strategy_id,
        )
        for trade, strategy_id in rows
    ]


async def _paper_executions(db: AsyncSession) -> list[UnifiedExecutionResponse]:
    rows = (
        await db.execute(
            select(PaperFill, StrategyVersion.strategy_id)
            .join(
                PaperTradingSubscription,
                PaperFill.subscription_id == PaperTradingSubscription.id,
            )
            .join(
                StrategyVersion,
                PaperTradingSubscription.strategy_version_id == StrategyVersion.id,
            )
            .order_by(PaperFill.created_at.desc())
        )
    ).all()
    return [
        UnifiedExecutionResponse(
            id=fill.id,
            mode="paper",
            time=fill.created_at.isoformat(),
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.filled_quantity,
            price=fill.avg_fill_price,
            status="filled" if fill.fully_filled else "partial",
            strategy_id=strategy_id,
        )
        for fill, strategy_id in rows
    ]


@router.get("")
async def list_orders_endpoint(
    mode: Literal["paper", "live", "both"] = "both",
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[UnifiedExecutionResponse]:
    executions: list[UnifiedExecutionResponse] = []
    if mode in ("paper", "both"):
        executions.extend(await _paper_executions(db))
    if mode in ("live", "both"):
        executions.extend(await _live_executions(db))
    executions.sort(key=lambda e: e.time, reverse=True)
    return executions
