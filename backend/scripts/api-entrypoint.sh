#!/bin/sh
# supervisord's "api" program command. Waits for the postgres program (the
# sibling process in this same container) to accept connections, runs
# migrations, then execs uvicorn. Uses asyncpg (an existing app dependency)
# for the readiness probe rather than assuming a particular postgres client
# binary is on PATH.
set -e

python - <<'PYEOF'
import asyncio
import os
import sys
import time

import asyncpg


async def wait_for_postgres() -> None:
    user = os.environ.get("POSTGRES_USER", "tradingos")
    password = os.environ.get("POSTGRES_PASSWORD", "tradingos")
    database = os.environ.get("POSTGRES_DB", "tradingos")
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(
                user=user, password=password, database=database, host="localhost", port=5432
            )
            await conn.close()
            return
        except Exception:
            await asyncio.sleep(1)

    print("[api] Postgres did not become ready in time", file=sys.stderr)
    sys.exit(1)


asyncio.run(wait_for_postgres())
PYEOF

echo "[api] running migrations..."
alembic upgrade head

echo "[api] starting uvicorn..."
exec uvicorn src.main:app --host 0.0.0.0 --port 8000
