"""E2E test-fixture seeding: more instruments than one page of the console holds.

e2e/paged-lists.spec.ts checks that Market Analysis searches the instrument master on the server (a
list of 50 with the total, narrowed by what is typed) and shows an underlying's option contracts a
page at a time (100). So there are 130 equities, whose symbols share a prefix of their own, and 120
option contracts on one underlying of its own. Both carry a random suffix, so a rerun against the
same database adds a second set, not a clash.

Usage:
    python scripts/seed_e2e_instruments.py
"""

import asyncio
import uuid
from datetime import date

from src.core.db import get_session_factory
from src.models.instrument import Instrument

EQUITIES = 130
CONTRACTS = 120


async def seed() -> None:
    suffix = uuid.uuid4().hex[:5].upper()
    equity_prefix = f"E2EEQ{suffix}"
    underlying = f"E2EUND{suffix}"
    async with get_session_factory()() as db:
        for i in range(EQUITIES):
            db.add(
                Instrument(
                    symbol=f"{equity_prefix}{i:03d}", exchange="NSE", instrument_type="equity"
                )
            )
        for i in range(CONTRACTS):
            strike = 20000 + 50 * (i // 2)
            kind = "CE" if i % 2 == 0 else "PE"
            db.add(
                Instrument(
                    symbol=f"{underlying}26NOV{strike}{kind}",
                    exchange="NFO",
                    instrument_type="option",
                    underlying_symbol=underlying,
                    expiry_date=date(2026, 11, 26),
                    strike_price=float(strike),
                    option_type=kind,
                )
            )
        await db.commit()
    print(f"equity_prefix={equity_prefix}")
    print(f"equities={EQUITIES}")
    print(f"underlying={underlying}")
    print(f"contracts={CONTRACTS}")


if __name__ == "__main__":
    asyncio.run(seed())
