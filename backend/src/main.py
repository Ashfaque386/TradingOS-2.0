import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agents.scheduler import start_heartbeat_loop
from src.api.router import api_router
from src.api.routes.health import router as health_router
from src.core.config import get_settings
from src.core.db import AsyncSessionLocal
from src.core.redis_client import get_redis
from src.gateway.apply import apply_config_from_file
from src.gateway.watcher import ConfigWatcher
from src.models.agent_config_version import ConfigVersionStatus
from src.observability.logging import configure_logging
from src.orchestration.recovery import reap_incomplete_runs
from src.orchestration.task_engine import drive_run_to_quiescence, start_stall_sweep_loop

configure_logging()

settings = get_settings()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config_path = Path(settings.agent_gateway_config_path)

    async with AsyncSessionLocal() as session:
        result = await apply_config_from_file(session, config_path, source="startup")

    if result.status == ConfigVersionStatus.ACTIVE:
        logger.info("agent_gateway.startup_config_applied", version=result.version_id)
    else:
        # Never crash the app over a bad seed config — it starts with no
        # gateway state loaded (matches hot-reload's "keep running" rule).
        logger.warning(
            "agent_gateway.startup_config_rejected",
            version=result.version_id,
            errors=result.errors,
        )

    watcher = ConfigWatcher(config_path)
    watcher.start()

    redis = get_redis()
    async with AsyncSessionLocal() as session:
        reaped = await reap_incomplete_runs(session, redis)

    recovery_tasks: list[asyncio.Task] = []
    if reaped:
        logger.info("orchestration.runs_reaped", run_ids=[str(r) for r in reaped])
        # Reaping only makes stuck tasks reclaimable; it doesn't itself
        # resume execution. drive_run_to_quiescence() safely no-ops for a
        # reaped run that isn't RUNNING (e.g. PLANNING, whose re-entry is a
        # narrower crash window not yet handled here), so it's safe to call
        # for every reaped run unconditionally. Fired as background tasks
        # (not awaited) so app startup — and the /health endpoint — isn't
        # blocked on however long recovery takes.
        recovery_tasks = [
            asyncio.create_task(drive_run_to_quiescence(AsyncSessionLocal, redis, run_id))
            for run_id in reaped
        ]

    stall_sweep_task = start_stall_sweep_loop(AsyncSessionLocal, redis)
    heartbeat_task = start_heartbeat_loop(AsyncSessionLocal)

    yield

    stall_sweep_task.cancel()
    heartbeat_task.cancel()
    for task in recovery_tasks:
        task.cancel()
    watcher.stop()


app = FastAPI(title="TradingOS 2.0 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(api_router)
