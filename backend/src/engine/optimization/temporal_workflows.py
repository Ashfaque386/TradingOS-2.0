"""Temporal workflow/activity wrapper around `run_monte_carlo` (Build Spec
§10: Monte Carlo simulation is "distributed via Temporal"). Real,
production-intended code -- **never exercised in this repo's own tests or
CI**: this sandbox has no reachable Temporal server (pulling the
`temporalio/temporal` dev-server image here was blocked by this
environment's egress policy on the image-layer CDN, confirmed live, not
assumed), so nothing here has been run against a real Temporal cluster.
Same honest "real code path, unexercised without the real infra" posture
as `src.engine.sandbox.gvisor_runtime` (Phase 4) and
`src.agents.llm_router`'s provider HTTP clients (Phase 3). A single-node
Temporal dev server is wired into `docker-compose.yml` (service
`temporal`, `temporal server start-dev`) for local/production use, and
`src/workers/temporal_worker.py` is the worker process that runs this
workflow/activity against it.

The activity below is a thin wrapper, not a reimplementation: it just
calls the pure, already-tested, synchronous `run_monte_carlo()`. Temporal
supplies retries, distribution across worker processes, and durability;
none of the statistical logic lives here or is duplicated here.
"""

from dataclasses import asdict
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from src.engine.optimization.monte_carlo import run_monte_carlo

MONTE_CARLO_TASK_QUEUE = "backtest-monte-carlo"


@activity.defn
async def run_monte_carlo_activity(
    trade_pnls: list[float], n_paths: int, initial_capital: float, seed: int | None
) -> dict:
    result = run_monte_carlo(
        trade_pnls, n_paths=n_paths, initial_capital=initial_capital, seed=seed
    )
    return asdict(result)


@workflow.defn
class MonteCarloWorkflow:
    @workflow.run
    async def run(
        self,
        trade_pnls: list[float],
        n_paths: int = 10_000,
        initial_capital: float = 100_000.0,
        seed: int | None = None,
    ) -> dict:
        return await workflow.execute_activity(
            run_monte_carlo_activity,
            args=[trade_pnls, n_paths, initial_capital, seed],
            start_to_close_timeout=timedelta(minutes=10),
        )
