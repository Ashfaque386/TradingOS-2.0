"""E2E test-fixture seeding: more finished runs than one page of Mission Control holds.

e2e/paged-lists.spec.ts checks that Mission Control shows the runs a page at a time (50) with the
total and a pager. Starting that many runs through the app would run the whole agent pipeline for
each, so the finished runs are written directly. They are dated two days back, older than any run
another spec makes, so they are the oldest runs in the database and push none of those off the first
page: the newest of them sits just after the others, and the oldest is on the last page. A spec that
used them removes them again (`cleanup`), so the runs a later spec makes are the only ones it sees.

Usage:
    python scripts/seed_e2e_runs.py [count]
    python scripts/seed_e2e_runs.py open        # one run paused for sign-off, three days old
    python scripts/seed_e2e_runs.py cleanup <prefix>
"""

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from src.core.db import get_session_factory
from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType


async def seed(count: int) -> None:
    prefix = f"E2E paged run {uuid.uuid4().hex[:6]}"
    start = datetime.now(UTC) - timedelta(days=2)
    async with get_session_factory()() as db:
        for i in range(count):
            created = start + timedelta(seconds=i)
            db.add(
                OrganizationRun(
                    objective=f"{prefix} #{i:03d}",
                    source=RunSource.UI,
                    run_type=RunType.STANDARD,
                    status=RunStatus.COMPLETED,
                    created_at=created,
                    started_at=created,
                    completed_at=created,
                )
            )
        await db.commit()
    print(f"prefix={prefix}")
    print(f"count={count}")
    print(f"newest={prefix} #{count - 1:03d}")
    print(f"oldest={prefix} #000")


async def open_run() -> None:
    """A run waiting for a person, older than every run `seed` writes: the board must show it
    whatever page of history is open."""
    objective = f"E2E old open run {uuid.uuid4().hex[:6]}"
    created = datetime.now(UTC) - timedelta(days=3)
    async with get_session_factory()() as db:
        db.add(
            OrganizationRun(
                objective=objective,
                source=RunSource.UI,
                run_type=RunType.STANDARD,
                status=RunStatus.PAUSED,
                created_at=created,
                started_at=created,
            )
        )
        await db.commit()
    print(f"objective={objective}")


async def cleanup(prefix: str) -> None:
    async with get_session_factory()() as db:
        result = await db.execute(
            delete(OrganizationRun).where(OrganizationRun.objective.startswith(prefix))
        )
        await db.commit()
    print(f"removed={getattr(result, 'rowcount', 0)}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "cleanup":
        asyncio.run(cleanup(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "open":
        asyncio.run(open_run())
    else:
        asyncio.run(seed(int(sys.argv[1]) if len(sys.argv) > 1 else 120))
