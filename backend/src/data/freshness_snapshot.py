"""The real `DataLakeFreshnessCheck` implementation (Build Spec §14),
replacing the Phase 5 `FakeDataLakeFreshness` stand-in at every production
call site: `src.engine.backtest.freshness.check_data_freshness` takes any
object satisfying the `DataLakeFreshnessCheck` Protocol
(`has_data_for(symbol, day) -> bool`) -- this module is the real one,
backed by `dataset_freshness_records` (Postgres, not a live DuckDB query,
so a gate check is a fast async-native query with no risk of colliding
with the nightly catalog refresh job's brief write-lock on `catalog.duckdb`).

`FakeDataLakeFreshness` itself is untouched and still lives in
`src.engine.backtest.freshness` -- it remains a legitimate, fast,
dependency-free fixture for tests that don't need a real database, exactly
like every other Fake-prefixed stand-in in this codebase. What changes is
that no *production* code path constructs one anymore.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.dataset_freshness_record import DatasetFreshnessRecord


@dataclass(frozen=True, slots=True)
class DataLakeFreshnessSnapshot:
    """Satisfies `src.engine.backtest.freshness.DataLakeFreshnessCheck`.
    A snapshot, not a live query, so the freshness gate's `has_data_for`
    call stays synchronous -- the async DB read happens once, up front, in
    `load_freshness_snapshot`."""

    available_dates: dict[str, frozenset[date]]

    def has_data_for(self, symbol: str, day: date) -> bool:
        return day in self.available_dates.get(symbol, frozenset())


async def load_freshness_snapshot(
    db: AsyncSession, symbols: list[str], *, data_type: str = "daily_ohlcv"
) -> DataLakeFreshnessSnapshot:
    if not symbols:
        return DataLakeFreshnessSnapshot(available_dates={})
    result = await db.execute(
        select(DatasetFreshnessRecord.symbol, DatasetFreshnessRecord.data_date).where(
            DatasetFreshnessRecord.symbol.in_(symbols),
            DatasetFreshnessRecord.data_type == data_type,
        )
    )
    available: dict[str, set[date]] = {}
    for symbol, data_date in result.all():
        available.setdefault(symbol, set()).add(data_date)
    return DataLakeFreshnessSnapshot(
        available_dates={symbol: frozenset(dates) for symbol, dates in available.items()}
    )
