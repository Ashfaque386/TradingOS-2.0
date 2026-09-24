"""NSE Bhavcopy EOD / F&O Bhavcopy (Build Spec §14): the **on-demand
fallback** ingestion path, used when the primary provider-driven
incremental pipeline needs a second source (or when an operator just wants
NSE's own official daily file for a given date).

This is the one Phase 10 pipeline that genuinely attempts a real network
call, matching Phase 8's Upstox Shadow Mode precedent (a real
sandbox-pointed dry run, honestly attempted, versus Zerodha's
local-payload-only path): `fetch_bhavcopy` really calls NSE's public
archive over `httpx`. This sandbox's own egress policy has failed every
prior phase's real broker network attempt (Zerodha, Upstox) with a 403 --
the same is expected here. On any failure (network, non-2xx, unparseable
body) this returns `source="nse_bhavcopy_unavailable"` with the error
captured, rather than raising; the caller
(`src.orchestration.market_data.run_bhavcopy_fallback`) then falls back to
the deterministic synthetic provider, tagging provenance honestly so
"real official data" and "synthetic stand-in" are never confused in the
stored record -- the same distinction Shadow Mode's `confidence` field
draws.
"""

from dataclasses import dataclass
from datetime import date

import httpx
import structlog

logger = structlog.get_logger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

_EQUITY_BHAVCOPY_URL = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)
_FO_BHAVCOPY_URL = "https://archives.nseindia.com/content/fo/fo{ddmmyyyy}bhav.csv"


@dataclass(frozen=True, slots=True)
class BhavcopyRow:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True, slots=True)
class BhavcopyFetchResult:
    source: str  # "nse_bhavcopy_real" | "nse_bhavcopy_unavailable"
    rows: tuple[BhavcopyRow, ...]
    error: str | None


def _parse_equity_csv(text: str) -> list[BhavcopyRow]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [h.strip().upper() for h in lines[0].split(",")]
    try:
        idx = {
            "symbol": header.index("SYMBOL"),
            "date": header.index("DATE1"),
            "open": header.index("OPEN_PRICE"),
            "high": header.index("HIGH_PRICE"),
            "low": header.index("LOW_PRICE"),
            "close": header.index("CLOSE_PRICE"),
            "volume": header.index("TTL_TRD_QNTY"),
        }
    except ValueError:
        return []

    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) <= max(idx.values()):
            continue
        try:
            rows.append(
                BhavcopyRow(
                    symbol=cells[idx["symbol"]],
                    trade_date=date.fromisoformat(_normalize_date(cells[idx["date"]])),
                    open=float(cells[idx["open"]]),
                    high=float(cells[idx["high"]]),
                    low=float(cells[idx["low"]]),
                    close=float(cells[idx["close"]]),
                    volume=int(float(cells[idx["volume"]])),
                )
            )
        except (ValueError, IndexError):
            continue
    return rows


def _normalize_date(raw: str) -> str:
    # NSE's DATE1 column is DD-MON-YYYY (e.g. "17-SEP-2026"); normalize to
    # ISO for date.fromisoformat.
    from datetime import datetime

    return datetime.strptime(raw, "%d-%b-%Y").date().isoformat()


def _parse_fo_csv(text: str) -> list[BhavcopyRow]:
    # F&O bhavcopy carries one row per contract (SYMBOL, EXPIRY_DT,
    # STRIKE_PR, OPTION_TYP, OPEN, HIGH, LOW, CLOSE, CONTRACTS, ...) --
    # this pulls just the OHLC-per-contract-row shape this build's
    # `BhavcopyRow` needs; per-contract fields (expiry, strike, option
    # type) aren't modeled here since nothing in this codebase consumes
    # F&O bhavcopy data yet beyond the same generic OHLC ingestion path.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [h.strip().upper() for h in lines[0].split(",")]
    try:
        idx = {
            "symbol": header.index("SYMBOL"),
            "open": header.index("OPEN"),
            "high": header.index("HIGH"),
            "low": header.index("LOW"),
            "close": header.index("CLOSE"),
            "volume": header.index("CONTRACTS"),
            "timestamp": header.index("TIMESTAMP"),
        }
    except ValueError:
        return []

    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) <= max(idx.values()):
            continue
        try:
            rows.append(
                BhavcopyRow(
                    symbol=cells[idx["symbol"]],
                    trade_date=date.fromisoformat(_normalize_date(cells[idx["timestamp"]])),
                    open=float(cells[idx["open"]]),
                    high=float(cells[idx["high"]]),
                    low=float(cells[idx["low"]]),
                    close=float(cells[idx["close"]]),
                    volume=int(float(cells[idx["volume"]])),
                )
            )
        except (ValueError, IndexError):
            continue
    return rows


async def fetch_bhavcopy(
    day: date, *, segment: str = "equity", transport: httpx.AsyncBaseTransport | None = None
) -> BhavcopyFetchResult:
    if segment == "fo":
        url = _FO_BHAVCOPY_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
        parser = _parse_fo_csv
    else:
        url = _EQUITY_BHAVCOPY_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
        parser = _parse_equity_csv
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=15.0, headers={"User-Agent": _USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            rows = parser(response.text)
        if not rows:
            return BhavcopyFetchResult(
                source="nse_bhavcopy_unavailable", rows=(), error="fetched body parsed to zero rows"
            )
        logger.info(
            "bhavcopy.real_fetch_succeeded", day=day.isoformat(), segment=segment, rows=len(rows)
        )
        return BhavcopyFetchResult(source="nse_bhavcopy_real", rows=tuple(rows), error=None)
    except Exception as exc:  # noqa: BLE001 - any failure degrades to the synthetic fallback, never raises
        logger.warning(
            "bhavcopy.real_fetch_failed", day=day.isoformat(), segment=segment, error=str(exc)
        )
        return BhavcopyFetchResult(source="nse_bhavcopy_unavailable", rows=(), error=str(exc))
