"""Heartbeat scheduler (Build Spec §17): a configurable-interval background
loop that runs src.agents.heartbeat.run_heartbeat for every currently
eligible agent. Same "asyncio.Task started from FastAPI's lifespan,
cancelled on shutdown" shape as
src.orchestration.task_engine.start_stall_sweep_loop -- deliberately
imports nothing from src.execution or src.risk either, for the same
structural-isolation reason as src.agents.heartbeat.
"""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.heartbeat import eligible_agents, run_heartbeat
from src.observability.correlation import bind_job_correlation_id

logger = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300


async def run_heartbeat_sweep_once(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    checked: list[str] = []
    for agent_id in sorted(eligible_agents()):
        async with session_factory() as db:
            await run_heartbeat(db, agent_id)
        checked.append(agent_id)
    return checked


def start_heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> asyncio.Task:
    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            async with bind_job_correlation_id("heartbeat_sweep"):
                try:
                    await run_heartbeat_sweep_once(session_factory)
                except Exception:  # noqa: BLE001 - a sweep failure must never kill the loop
                    logger.exception("agents.heartbeat.sweep_failed")

    return asyncio.create_task(_loop())
