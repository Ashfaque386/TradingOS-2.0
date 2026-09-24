"""Temporal worker process for the Monte Carlo workflow (Build Spec §10).
Run against the `temporal` docker-compose service (or a local dev server,
via `TEMPORAL_ADDRESS`):

    python -m src.workers.temporal_worker

Never started automatically by the API process or by the test suite --
this sandbox has no reachable Temporal server (see
src/engine/optimization/temporal_workflows.py's docstring for what was
actually tried and why). A production deployment runs this as its own
container/process alongside the API, connecting to the same Temporal
cluster docker-compose.yml's `temporal` service provides.
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from src.core.config import get_settings
from src.engine.optimization.temporal_workflows import (
    MONTE_CARLO_TASK_QUEUE,
    MonteCarloWorkflow,
    run_monte_carlo_activity,
)


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address)
    worker = Worker(
        client,
        task_queue=MONTE_CARLO_TASK_QUEUE,
        workflows=[MonteCarloWorkflow],
        activities=[run_monte_carlo_activity],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
