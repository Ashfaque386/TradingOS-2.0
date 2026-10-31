"""What a mode's account is worth, and the history of it (Phase 3).

Equity is built only from numbers this system holds itself: the capital the account started with,
what it has realized (net of costs), and what its open positions are worth at the latest price it
has seen. When any of those cannot be known -- an open position nobody has priced, or (live) no
starting capital configured -- the reading is NULL with a note saying why. It is never filled in
with a zero or a guess, because the automatic kill switch acts on these readings: a gap that
looked like a loss would stop trading for nothing, and one that looked like a recovery would hide
a real loss.

Paper starts from the sum of every paper subscription's `initial_capital`. A subscription added
later raises the capital and the equity by the same amount, so it moves neither the peak nor the
drawdown.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.brokers.tick_source import is_broker_configured
from src.core.config import get_settings
from src.engine.paper_trading.tick_feed import get_latest_tick
from src.engine.risk.kill_switch import KillSwitchMode
from src.models.equity_snapshot import EquitySnapshot
from src.models.live_position import LivePosition
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription

__all__ = [
    "EquitySample",
    "QuoteFn",
    "adapter_quote",
    "configured_live_quote",
    "measure_equity",
    "measure_live_equity",
    "measure_paper_equity",
    "paper_price_source",
    "peak_equity_since",
    "record_snapshot",
]

PriceSource = Literal["real", "synthetic", "none"]

# The latest price for a symbol, or None when it cannot be had.
QuoteFn = Callable[[str], Awaitable[float | None]]


def paper_price_source() -> PriceSource:
    """Where paper positions are priced from right now: a connected broker's quotes, the
    generated demo walk when that is switched on, or nothing."""
    if is_broker_configured():
        return "real"
    return "synthetic" if get_settings().paper_trading_synthetic else "none"


@dataclass(frozen=True, slots=True)
class EquitySample:
    """One measurement, before it is stored. `equity` is None when it could not be measured."""

    mode: KillSwitchMode
    equity: float | None
    capital_base: float | None
    realized_net: float | None
    unrealized: float | None
    open_positions: int
    unpriced_positions: int
    price_source: PriceSource
    note: str | None


async def measure_paper_equity(
    db: AsyncSession, redis: Redis, *, price_source: PriceSource
) -> EquitySample:
    """Capital + realized net of costs + unrealized at the latest tick. An open position with no
    tick makes the unrealized part, and so the equity, unknown."""
    capital = (
        await db.execute(
            select(func.coalesce(func.sum(PaperTradingSubscription.initial_capital), 0.0))
        )
    ).scalar_one()
    realized_gross, costs = (
        await db.execute(
            select(
                func.coalesce(func.sum(PaperPosition.realized_pnl), 0.0),
                func.coalesce(func.sum(PaperPosition.costs_total), 0.0),
            )
        )
    ).one()
    realized_net = float(realized_gross) - float(costs)

    open_positions = (
        (await db.execute(select(PaperPosition).where(PaperPosition.quantity != 0))).scalars().all()
    )
    unpriced = 0
    unrealized = 0.0
    for position in open_positions:
        tick = await get_latest_tick(redis, position.symbol)
        if tick is None:
            unpriced += 1
            continue
        unrealized += (tick.price - position.avg_cost) * position.quantity

    if float(capital) <= 0:
        return EquitySample(
            mode="paper",
            equity=None,
            capital_base=float(capital),
            realized_net=realized_net,
            unrealized=None,
            open_positions=len(open_positions),
            unpriced_positions=unpriced,
            price_source=price_source,
            note="no paper subscription has any capital, so there is nothing to measure against",
        )
    if unpriced:
        return EquitySample(
            mode="paper",
            equity=None,
            capital_base=float(capital),
            realized_net=realized_net,
            unrealized=None,
            open_positions=len(open_positions),
            unpriced_positions=unpriced,
            price_source=price_source,
            note=f"{unpriced} open position(s) have no price, so the equity is unknown",
        )
    return EquitySample(
        mode="paper",
        equity=float(capital) + realized_net + unrealized,
        capital_base=float(capital),
        realized_net=realized_net,
        unrealized=unrealized,
        open_positions=len(open_positions),
        unpriced_positions=0,
        price_source=price_source,
        note=None,
    )


async def peak_equity_since(
    db: AsyncSession, mode: KillSwitchMode, since: datetime | None
) -> float | None:
    """The highest equity recorded for `mode` since `since` (None: ever). A human reset of the
    kill switch re-bases the peak: the monitor passes the reset time, so the drawdown that tripped
    the switch does not trip it again the moment it is re-armed."""
    query = select(func.max(EquitySnapshot.equity)).where(
        EquitySnapshot.mode == mode, EquitySnapshot.equity.is_not(None)
    )
    if since is not None:
        query = query.where(EquitySnapshot.taken_at >= since)
    return (await db.execute(query)).scalar_one()


def record_snapshot(
    db: AsyncSession,
    sample: EquitySample,
    *,
    peak_equity: float | None,
    drawdown_pct: float | None,
    taken_at: datetime | None = None,
) -> EquitySnapshot:
    """Add the reading to the session (the caller commits, in the same transaction as whatever
    the monitor decided from it)."""
    snapshot = EquitySnapshot(
        mode=sample.mode,
        taken_at=taken_at or datetime.now(UTC),
        equity=sample.equity,
        capital_base=sample.capital_base,
        realized_net=sample.realized_net,
        unrealized=sample.unrealized,
        open_positions=sample.open_positions,
        unpriced_positions=sample.unpriced_positions,
        price_source=sample.price_source,
        peak_equity=peak_equity,
        drawdown_pct=drawdown_pct,
        note=sample.note,
    )
    db.add(snapshot)
    return snapshot


def adapter_quote(adapter: BrokerAdapter | None) -> QuoteFn:
    """Price live positions at the broker's own quote. No broker, or a quote that fails, is "no
    price" -- never a guess."""

    async def quote(symbol: str) -> float | None:
        if adapter is None:
            return None
        try:
            return float((await adapter.get_quote(symbol)).last_price)
        except Exception:  # noqa: BLE001 - any failure to quote is simply an unpriced position
            return None

    return quote


def configured_live_quote() -> QuoteFn:
    """A quote function for whichever broker is connected right now (resolved at each call, so a
    broker connected or disconnected later takes effect at the next reading)."""
    return adapter_quote(build_configured_adapter(sandbox=False))


async def measure_live_equity(
    db: AsyncSession, *, starting_capital: float | None, quote: QuoteFn
) -> EquitySample:
    """The starting capital plus the confirmed-fill ledger's realized result (net of estimated
    costs) plus every open position at the broker's quote. Without a starting capital, or with an
    open position that cannot be priced, the equity is unknown."""
    rows = (await db.execute(select(LivePosition))).scalars().all()
    realized_net = sum(row.realized_pnl - row.costs_total for row in rows)
    open_rows = [row for row in rows if row.quantity != 0]

    if starting_capital is None or starting_capital <= 0:
        return EquitySample(
            mode="live",
            equity=None,
            capital_base=None,
            realized_net=realized_net,
            unrealized=None,
            open_positions=len(open_rows),
            unpriced_positions=0,
            price_source="none",
            note=(
                "LIVE_STARTING_CAPITAL is not set, so live equity has nothing to be measured "
                "against"
            ),
        )

    unrealized = 0.0
    unpriced = 0
    for row in open_rows:
        price = await quote(row.symbol)
        if price is None:
            unpriced += 1
            continue
        unrealized += (price - row.avg_cost) * row.quantity

    if unpriced:
        return EquitySample(
            mode="live",
            equity=None,
            capital_base=starting_capital,
            realized_net=realized_net,
            unrealized=None,
            open_positions=len(open_rows),
            unpriced_positions=unpriced,
            price_source="real",
            note=f"{unpriced} open live position(s) could not be priced, so the equity is unknown",
        )
    return EquitySample(
        mode="live",
        equity=starting_capital + realized_net + unrealized,
        capital_base=starting_capital,
        realized_net=realized_net,
        unrealized=unrealized,
        open_positions=len(open_rows),
        unpriced_positions=0,
        price_source="real",
        note=None,
    )


async def measure_equity(
    db: AsyncSession, redis: Redis, mode: KillSwitchMode, *, live_quote: QuoteFn | None = None
) -> EquitySample:
    """The current reading for `mode`. Live positions are priced with `live_quote` (the connected
    broker's quote when none is given)."""
    if mode == "paper":
        return await measure_paper_equity(db, redis, price_source=paper_price_source())
    return await measure_live_equity(
        db,
        starting_capital=get_settings().live_starting_capital,
        quote=live_quote or configured_live_quote(),
    )
