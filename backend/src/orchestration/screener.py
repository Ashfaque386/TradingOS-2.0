"""Screener Agent (Phase 19, docs/phase19-audit.md Part 2): a configurable
filter run on a schedule against the real Phase 10 instrument master and
real OHLCV bars, surfacing candidates directly into the existing Strategy
Generator's real entrypoint (`run_strategy_pipeline`) -- never a separate,
disconnected candidate list nobody consumes.

Deliberately does NOT filter by sector or a market-cap proxy: the Phase 19
audit confirmed neither field exists anywhere in this codebase (`Instrument`
has no sector/market-cap column, and no other data source carries one) --
fabricating either would violate this codebase's established "never
fabricate a metric" rule (the same rule the backtest engine and Market
Analysis page already follow). Filters here are only ones backed by real
data: instrument type (the real `Instrument` table), liquidity (average
real volume from `daily_bars`), and a basic technical criterion (last close
vs. its own trailing SMA over the same window) -- all computable from data
this codebase actually has.
"""

from dataclasses import dataclass

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.instrument import Instrument
from src.models.strategy import Strategy
from src.orchestration.strategies import run_strategy_pipeline

logger = structlog.get_logger(__name__)

DEFAULT_MIN_AVG_VOLUME = 10_000.0
DEFAULT_LOOKBACK_DAYS = 20
DEFAULT_MAX_CANDIDATES_PER_RUN = 3
SCREENER_ACTOR = "system:screener-agent"


@dataclass(frozen=True, slots=True)
class ScreenerFilters:
    instrument_types: tuple[str, ...] = ("equity",)
    min_avg_volume: float = DEFAULT_MIN_AVG_VOLUME
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    # Last close above its own trailing SMA over `lookback_days` -- a
    # momentum/uptrend proxy computable from real OHLCV, not a fabricated
    # signal.
    require_uptrend: bool = True


@dataclass(frozen=True, slots=True)
class ScreenResult:
    symbol: str
    avg_volume: float
    last_close: float
    sma: float
    passed: bool
    reasons: tuple[str, ...]


async def screen_instruments(
    db: AsyncSession,
    price_provider: PriceDataProvider,
    *,
    as_of: pd.Timestamp,
    filters: ScreenerFilters = ScreenerFilters(),
) -> list[ScreenResult]:
    """Runs the real filter against every active instrument of the
    configured type(s) in the real instrument master. Returns every
    instrument checked, passed or not, with its real numbers and the exact
    reason(s) it failed -- never silently drops a candidate without saying
    why."""
    result = await db.execute(
        select(Instrument).where(
            Instrument.is_active.is_(True),
            Instrument.instrument_type.in_(filters.instrument_types),
        )
    )
    instruments = result.scalars().all()

    results: list[ScreenResult] = []
    for instrument in instruments:
        bars = price_provider.daily_bars(
            instrument.symbol, as_of=as_of, lookback_days=filters.lookback_days
        )
        if bars.empty:
            results.append(
                ScreenResult(instrument.symbol, 0.0, 0.0, 0.0, False, ("no price data",))
            )
            continue

        avg_volume = float(bars["volume"].mean())
        last_close = float(bars["close"].iloc[-1])
        sma = float(bars["close"].mean())

        reasons: list[str] = []
        if avg_volume < filters.min_avg_volume:
            reasons.append(
                f"avg volume {avg_volume:.0f} below minimum {filters.min_avg_volume:.0f}"
            )
        if filters.require_uptrend and last_close <= sma:
            reasons.append(f"last close {last_close:.2f} not above trailing SMA {sma:.2f}")

        results.append(
            ScreenResult(
                instrument.symbol, avg_volume, last_close, sma, len(reasons) == 0, tuple(reasons)
            )
        )
    return results


async def run_screener_and_generate_strategies(
    db: AsyncSession,
    price_provider: PriceDataProvider,
    *,
    as_of: pd.Timestamp,
    filters: ScreenerFilters = ScreenerFilters(),
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
    created_by: str = SCREENER_ACTOR,
) -> list[str]:
    """The real feedback loop: passing candidates become a real strategy
    objective fed into the exact same `run_strategy_pipeline()` entrypoint
    every other strategy-creation path in this codebase uses -- not a
    parallel mechanism. Skips a candidate that already has a strategy
    created by this same screener run today (idempotent under a scheduler
    that might fire more than once for the same `as_of`). Returns the
    names of any strategies created this call.
    """
    results = await screen_instruments(db, price_provider, as_of=as_of, filters=filters)
    passing = [r for r in results if r.passed][:max_candidates]

    created: list[str] = []
    for candidate in passing:
        name = f"Screener candidate: {candidate.symbol} ({as_of.date().isoformat()})"
        existing = await db.execute(select(Strategy).where(Strategy.name == name))
        if existing.scalars().first() is not None:
            continue

        objective = (
            f"Generate a trading strategy for {candidate.symbol}, surfaced by the "
            f"Screener Agent: average volume {candidate.avg_volume:.0f} over the "
            f"trailing {filters.lookback_days} trading days (minimum required "
            f"{filters.min_avg_volume:.0f}), last close {candidate.last_close:.2f} "
            f"above its own trailing SMA {candidate.sma:.2f}."
        )
        strategy, _version = await run_strategy_pipeline(
            db,
            name=name,
            objective=objective,
            created_by=created_by,
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
        created.append(strategy.name)
        logger.info(
            "screener.strategy_created",
            symbol=candidate.symbol,
            strategy_id=str(strategy.id),
            avg_volume=candidate.avg_volume,
        )

    if not passing:
        logger.info("screener.no_candidates_passed", checked=len(results))
    return created
