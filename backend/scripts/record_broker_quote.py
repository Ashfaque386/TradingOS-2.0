"""Records Upstox's quote answer exactly as the broker sent it, so the field that carries the
exchange's own timestamp can be settled from a real payload (`brokers.base._exchange_time` returns
nothing for Upstox until then: its documented fields disagree with one another, and guessing which
one is right is how a stale price gets acted on).

Run from backend/ (inside the backend container) with Upstox connected in Settings:

    python -m scripts.record_broker_quote out.json INFY RELIANCE

Record it twice: once with the market open and once after the close (a stamp that is from a previous
day is the case the guard exists for). Nothing secret is written: the file holds the instrument key,
the request path and the response body, never the token. Send both files back; the field that moves
with the exchange clock, and its format and zone, are then pinned by a test against them.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.brokers.base import BrokerCredentials
from src.brokers.upstox import UpstoxAdapter
from src.engine.paper_trading.market_hours import is_market_open_ist
from src.security.secrets_store import get_secrets_store

ENDPOINT = "/market-quote/quotes"


async def record_quotes(
    adapter: UpstoxAdapter, symbols: list[str], *, now: datetime
) -> dict[str, Any]:
    """The recording: when it was made, whether the NSE session was open, and per symbol the
    instrument key asked for and the response body untouched."""
    responses: dict[str, Any] = {}
    for symbol in symbols:
        key, body = await adapter.raw_quote(symbol)
        responses[symbol] = {"instrument_key": key, "response": body}
    return {
        "broker": "upstox",
        "endpoint": ENDPOINT,
        "captured_at_utc": now.astimezone(UTC).isoformat(),
        "nse_session_open": is_market_open_ist(now),
        "quotes": responses,
    }


async def main(out: Path, symbols: list[str]) -> None:
    credentials: BrokerCredentials | None = get_secrets_store().get_credentials("upstox")
    if credentials is None:
        raise SystemExit("Upstox is not connected: connect it in Settings first.")
    recording = await record_quotes(UpstoxAdapter(credentials), symbols, now=datetime.now(UTC))
    out.write_text(json.dumps(recording, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(symbols)} symbols, session open: {recording['nse_session_open']}).")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python -m scripts.record_broker_quote <out.json> <SYMBOL> [SYMBOL...]",
            file=sys.stderr,
        )
        raise SystemExit(1)

    asyncio.run(main(Path(sys.argv[1]), sys.argv[2:]))
