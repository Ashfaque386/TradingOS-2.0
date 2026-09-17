"""DuckDB + Parquet data lake (Build Spec §14, §5.2): OHLCV bars partitioned
`year/month/symbol.parquet`, with two DuckDB catalog views (`ohlcv_daily`,
`ohlcv_intraday`) refreshed on a schedule.

**Two access paths, deliberately separate**: every write and every
data-bearing read (`write_daily_bars`/`write_intraday_bars`,
`read_daily_bars`, `has_data_for`) goes straight at the Parquet files
through a short-lived **in-memory** DuckDB connection (`duckdb.connect(":memory:")`)
-- there is no persistent lock on the actual data. `refresh_catalog_views`
is the one function that opens the **persistent** `catalog.duckdb` file
(read_write, briefly, then closed) to (re)create the two named views for
an external DuckDB CLI/BI-tool consumer; nothing in this codebase's own
read path depends on that file being fresh or even present, so a
single-process app never risks a writer/reader file-lock conflict between
"a query running" and "the nightly catalog refresh job."

**Idempotent writes, by construction**: every partition write is a
read-merge-write -- read whatever's already in that partition file (if
any), concatenate the new rows, `drop_duplicates` on the natural key, sort,
write back. Writing the same bars twice never duplicates a row; writing an
overlapping-but-different batch (e.g. a corrected close price) never leaves
stale duplicate rows behind, since the later batch always wins
(`keep="last"`).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pandas as pd

DAILY_SUBDIR = "ohlcv_daily"
INTRADAY_SUBDIR = "ohlcv_intraday"
CATALOG_DB_FILENAME = "catalog.duckdb"

DAILY_OHLCV_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume"]
INTRADAY_OHLCV_COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]

_DUCKDB_TYPE = {
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "symbol": "VARCHAR",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "BIGINT",
}


@dataclass(frozen=True, slots=True)
class WriteResult:
    symbol: str
    rows_submitted: int
    rows_added: int
    partitions_written: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    ohlcv_daily_rows: int
    ohlcv_intraday_rows: int
    refreshed_at: datetime


def _partition_path(root: Path, subdir: str, symbol: str, year: int, month: int) -> Path:
    return root / subdir / f"{year:04d}" / f"{month:02d}" / f"{symbol}.parquet"


def _write_partitioned(
    root: Path, subdir: str, symbol: str, bars: pd.DataFrame, *, key_column: str
) -> WriteResult:
    if bars.empty:
        return WriteResult(symbol=symbol, rows_submitted=0, rows_added=0, partitions_written=())

    df = bars.copy()
    df.index.name = key_column
    df = df.reset_index()
    df["symbol"] = symbol
    parsed = pd.to_datetime(df[key_column])
    df[key_column] = parsed.dt.date if key_column == "date" else parsed

    partitions_written: list[Path] = []
    rows_added = 0
    for (year, month), group in df.groupby([parsed.dt.year, parsed.dt.month]):
        path = _partition_path(root, subdir, symbol, int(year), int(month))
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(":memory:")
        try:
            if path.exists():
                existing = con.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
                rows_before = len(existing)
                # DuckDB round-trips DATE/TIMESTAMP columns back as pandas
                # Timestamps regardless of what Python type was written --
                # re-normalize to match `group`'s dtype or the concat below
                # ends up with a mixed-type object column that sort_values
                # can't compare.
                existing_key = pd.to_datetime(existing[key_column])
                existing[key_column] = (
                    existing_key.dt.date if key_column == "date" else existing_key
                )
                merged = pd.concat([existing, group], ignore_index=True)
            else:
                rows_before = 0
                merged = group
            merged = merged.drop_duplicates(subset=[key_column, "symbol"], keep="last")
            merged = merged.sort_values(key_column).reset_index(drop=True)
            con.register("merged_view", merged)
            con.execute("COPY merged_view TO ? (FORMAT PARQUET)", [str(path)])
            rows_added += len(merged) - rows_before
        finally:
            con.close()
        partitions_written.append(path)

    return WriteResult(
        symbol=symbol,
        rows_submitted=len(df),
        rows_added=rows_added,
        partitions_written=tuple(partitions_written),
    )


def write_daily_bars(root: Path, symbol: str, bars: pd.DataFrame) -> WriteResult:
    """`bars` must have a DatetimeIndex and open/high/low/close/volume
    columns (the same shape `PriceDataProvider.daily_bars` returns)."""
    return _write_partitioned(root, DAILY_SUBDIR, symbol, bars, key_column="date")


def write_intraday_bars(root: Path, symbol: str, bars: pd.DataFrame) -> WriteResult:
    return _write_partitioned(root, INTRADAY_SUBDIR, symbol, bars, key_column="timestamp")


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def read_daily_bars(root: Path, symbol: str, start: date, end: date) -> pd.DataFrame:
    paths = [
        _partition_path(root, DAILY_SUBDIR, symbol, year, month)
        for year, month in _month_range(start, end)
    ]
    existing = [p for p in paths if p.exists()]
    if not existing:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    con = duckdb.connect(":memory:")
    try:
        frames = [con.execute("SELECT * FROM read_parquet(?)", [str(p)]).df() for p in existing]
    finally:
        con.close()
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[(df["symbol"] == symbol) & (df["date"] >= start) & (df["date"] <= end)]
    df = df.sort_values("date")
    df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["date"])))
    return df[["open", "high", "low", "close", "volume"]]


def has_data_for(root: Path, symbol: str, day: date, *, subdir: str = DAILY_SUBDIR) -> bool:
    """The real, ground-truth freshness check: does a readable row for this
    exact symbol/day exist in the lake right now? Used to confirm a write
    actually landed (see `src.orchestration.market_data`) rather than
    trusting the write call's own return value alone."""
    key_column = "date" if subdir == DAILY_SUBDIR else "timestamp"
    path = _partition_path(root, subdir, symbol, day.year, day.month)
    if not path.exists():
        return False
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"SELECT 1 FROM read_parquet(?) WHERE symbol = ? "
            f"AND CAST({key_column} AS DATE) = ? LIMIT 1",
            [str(path), symbol, day],
        ).fetchone()
    finally:
        con.close()
    return row is not None


def _create_or_replace_view(
    con: duckdb.DuckDBPyConnection, root: Path, subdir: str, view_name: str, columns: list[str]
) -> int:
    has_files = any((root / subdir).glob("*/*/*.parquet"))
    if has_files:
        glob = (root / subdir / "*" / "*" / "*.parquet").as_posix()
        con.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
        )
        return con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]  # type: ignore[index]

    # No partition files exist yet -- define an empty, correctly-typed view
    # rather than letting a bare read_parquet(glob) raise "no files found".
    col_defs = ", ".join(f"NULL::{_DUCKDB_TYPE[c]} AS {c}" for c in columns)
    con.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT {col_defs} WHERE FALSE")
    return 0


def refresh_catalog_views(root: Path) -> CatalogRefreshResult:
    root.mkdir(parents=True, exist_ok=True)
    catalog_path = root / CATALOG_DB_FILENAME
    con = duckdb.connect(str(catalog_path))
    try:
        daily_rows = _create_or_replace_view(
            con, root, DAILY_SUBDIR, "ohlcv_daily", DAILY_OHLCV_COLUMNS
        )
        intraday_rows = _create_or_replace_view(
            con, root, INTRADAY_SUBDIR, "ohlcv_intraday", INTRADAY_OHLCV_COLUMNS
        )
    finally:
        con.close()
    return CatalogRefreshResult(
        ohlcv_daily_rows=daily_rows,
        ohlcv_intraday_rows=intraday_rows,
        refreshed_at=datetime.now(UTC),
    )


def row_count(path: Path) -> int:
    con = duckdb.connect(":memory:")
    try:
        return con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]  # type: ignore[index]
    finally:
        con.close()


def all_parquet_files(root: Path) -> list[Path]:
    return sorted(root.glob("**/*.parquet"))
