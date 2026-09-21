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

if [ -n "$DEFAULT_ADMIN_EMAIL" ] && [ -n "$DEFAULT_ADMIN_PASSWORD" ]; then
    echo "[api] ensuring default admin user exists..."
    python -m scripts.create_admin "$DEFAULT_ADMIN_EMAIL" "$DEFAULT_ADMIN_PASSWORD"
fi

echo "[api] starting uvicorn..."
# UVICORN_HOST defaults to 0.0.0.0 -- required for backend/Dockerfile's own
# multi-container use (docker-compose publishes this container's 8000
# directly to the host, so uvicorn must accept that). The all-in-one image
# (Dockerfile.allinone) overrides this to 127.0.0.1: there, only its bundled
# nginx reverse proxy is meant to be reachable at all, so uvicorn stays
# loopback-only behind it, same internal-only posture as every other port
# in this project (Build Spec §21-22).
exec uvicorn src.main:app --host "${UVICORN_HOST:-0.0.0.0}" --port 8000
