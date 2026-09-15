from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.router import api_router
from src.api.routes.health import router as health_router
from src.core.config import get_settings
from src.core.db import AsyncSessionLocal
from src.gateway.apply import apply_config_from_file
from src.gateway.watcher import ConfigWatcher
from src.models.agent_config_version import ConfigVersionStatus
from src.observability.logging import configure_logging

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

    yield

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
