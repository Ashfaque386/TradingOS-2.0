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

The ledger is one SQL query over both tables (a UNION ALL), so a page of it, its total
and its filters are all decided by the database: a console can reach every execution, not
only the newest few hundred.

No "latency" field exists anywhere in either model -- that KPI is a real
Prometheus histogram (`tradingos_order_dispatch_latency_seconds`, Phase
11), not a per-row database column, so it is deliberately left off this
response rather than fabricated; the frontend should read it from
`GET /metrics` if it wants the aggregate figure.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    Float,
    Select,
    String,
    case,
    cast,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.paging import set_total_count
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy import Strategy
from src.models.strategy_version import StrategyVersion
from src.models.trade import Trade
from src.models.user import User

router = APIRouter(prefix="/orders", tags=["orders"])

register_policy("GET", "/api/v1/orders", roles=list(Role))
register_policy("GET", "/api/v1/orders/positions", roles=list(Role))


# Every status an execution can have: a paper fill is `filled` or `partial`; a live trade starts
# `pending_confirmation` and ends `filled`, `rejected`, `cancelled` or `failed`.
ExecutionStatus = Literal[
    "filled", "partial", "pending_confirmation", "rejected", "cancelled", "failed"
]


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
    strategy_name: str | None = None
    # What the price rests on (real, synthetic, mixed, supplied or unknown): a fill on the mock tick
    # walk must not read the same as one on a broker's quote.
    data_source: str = "unknown"
    # A live trade belongs to an order; a paper fill does not.
    order_id: uuid.UUID | None = None
    # What a paper fill cost (brokerage, taxes, charges). A live trade's costs are not recorded.
    costs: float | None = None
    # A paper close done by hand without the risk gate: not a trade the strategy made.
    administrative: bool = False


def _live_rows() -> Select[Any]:
    # SQLAlchemy 2.1 types a select by its columns and will not call thirteen of them `Select[Any]`
    # unasked; 2.0 did, so under 2.0 this ignore is unused (which is why it says so).
    return (
        select(  # type: ignore[return-value, unused-ignore]
            Trade.id.label("id"),
            literal("live", String).label("mode"),
            Trade.executed_at.label("time"),
            Trade.symbol.label("symbol"),
            Trade.side.label("side"),
            Trade.quantity.label("quantity"),
            func.coalesce(Trade.fill_price, Trade.price).label("price"),
            Trade.status.label("status"),
            Order.strategy_id.label("strategy_id"),
            Trade.data_source.label("data_source"),
            Trade.order_id.label("order_id"),
            cast(null(), Float).label("costs"),
            literal(False).label("administrative"),
        )
        .select_from(Trade)
        .join(Order, Trade.order_id == Order.id)
    )


def _paper_rows() -> Select[Any]:
    return (
        select(
            PaperFill.id.label("id"),
            literal("paper", String).label("mode"),
            PaperFill.created_at.label("time"),
            PaperFill.symbol.label("symbol"),
            PaperFill.side.label("side"),
            PaperFill.filled_quantity.label("quantity"),
            PaperFill.avg_fill_price.label("price"),
            case((PaperFill.fully_filled, "filled"), else_="partial").label("status"),
            StrategyVersion.strategy_id.label("strategy_id"),
            PaperFill.data_source.label("data_source"),
            cast(null(), PG_UUID(as_uuid=True)).label("order_id"),
            PaperFill.costs_total.label("costs"),
            PaperFill.administrative.label("administrative"),
        )
        .select_from(PaperFill)
        .join(PaperTradingSubscription, PaperFill.subscription_id == PaperTradingSubscription.id)
        .join(StrategyVersion, PaperTradingSubscription.strategy_version_id == StrategyVersion.id)
    )


@router.get("")
async def list_orders_endpoint(
    response: Response,
    mode: Literal["paper", "live", "both"] = "both",
    status_filter: ExecutionStatus | None = Query(default=None, alias="status"),
    symbol: str | None = Query(default=None, max_length=48),
    strategy_id: uuid.UUID | None = None,
    limit: int = Query(default=100, gt=0, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[UnifiedExecutionResponse]:
    """One page of the execution ledger, newest first. `X-Total-Count` is how many executions the
    filters match in all. `symbol` is a case-insensitive "contains"."""
    parts = []
    if mode in ("paper", "both"):
        parts.append(_paper_rows())
    if mode in ("live", "both"):
        parts.append(_live_rows())
    executions = union_all(*parts).subquery("executions")

    conditions: list[ColumnElement[bool]] = []
    if status_filter is not None:
        conditions.append(executions.c.status == status_filter)
    if symbol:
        conditions.append(executions.c.symbol.icontains(symbol, autoescape=True))
    if strategy_id is not None:
        conditions.append(executions.c.strategy_id == strategy_id)

    total = (
        await db.execute(select(func.count()).select_from(executions).where(*conditions))
    ).scalar_one()
    set_total_count(response, total)

    rows = (
        await db.execute(
            select(executions, Strategy.name.label("strategy_name"))
            .outerjoin(Strategy, Strategy.id == executions.c.strategy_id)
            .where(*conditions)
            .order_by(executions.c.time.desc(), executions.c.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [
        UnifiedExecutionResponse(
            id=row.id,
            mode=row.mode,
            time=row.time.isoformat(),
            symbol=row.symbol,
            side=row.side,
            quantity=row.quantity,
            price=row.price,
            status=row.status,
            strategy_id=row.strategy_id,
            strategy_name=row.strategy_name,
            data_source=row.data_source,
            order_id=row.order_id,
            costs=row.costs,
            administrative=row.administrative,
        )
        for row in rows
    ]


class PositionByStrategyResponse(BaseModel):
    mode: Literal["paper", "live"]
    strategy_id: uuid.UUID
    strategy_name: str
    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float
    # False for a paper position whose subscription is inactive: nothing is watching its
    # stop-loss or acting on signals for it. (Live positions are always managed.)
    managed: bool = True


@router.get("/positions")
async def list_positions_by_strategy_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[PositionByStrategyResponse]:
    """The aggregate the Orders & Trades page's "Positions by strategy"
    stat previously had no backend for -- both position tables already
    carry a real strategy linkage (LivePosition.strategy_id directly,
    PaperPosition by way of its subscription's strategy_version), this
    just joins each to Strategy.name and returns every non-flat position,
    live and paper together. A flat (quantity == 0) row is closed, not a
    position, so it's filtered out here rather than left for the frontend
    to notice.
    """
    live_rows = (
        await db.execute(
            select(LivePosition, Strategy.name)
            .join(Strategy, LivePosition.strategy_id == Strategy.id)
            .where(LivePosition.quantity != 0)
        )
    ).all()
    paper_rows = (
        await db.execute(
            select(PaperPosition, Strategy.id, Strategy.name, PaperTradingSubscription.is_active)
            .join(
                PaperTradingSubscription,
                PaperPosition.subscription_id == PaperTradingSubscription.id,
            )
            .join(
                StrategyVersion,
                PaperTradingSubscription.strategy_version_id == StrategyVersion.id,
            )
            .join(Strategy, StrategyVersion.strategy_id == Strategy.id)
            .where(PaperPosition.quantity != 0)
        )
    ).all()

    positions = [
        PositionByStrategyResponse(
            mode="live",
            strategy_id=position.strategy_id,
            strategy_name=name,
            symbol=position.symbol,
            quantity=position.quantity,
            avg_cost=position.avg_cost,
            realized_pnl=position.realized_pnl,
        )
        for position, name in live_rows
    ]
    positions.extend(
        PositionByStrategyResponse(
            mode="paper",
            strategy_id=strategy_id,
            strategy_name=name,
            symbol=position.symbol,
            quantity=position.quantity,
            avg_cost=position.avg_cost,
            realized_pnl=position.realized_pnl,
            managed=is_active,
        )
        for position, strategy_id, name, is_active in paper_rows
    )
    return positions
