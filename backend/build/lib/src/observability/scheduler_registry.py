"""A process-wide registry of every real APScheduler instance this app
starts (Phase 19, docs/phase19-audit.md Part 1.2's central finding: 15
real scheduled jobs across 6 schedulers existed, but nothing anywhere
could enumerate them). `src.main`'s lifespan registers each scheduler
here as it starts; `GET /api/v1/system/scheduled-jobs` reads real,
live `next_run_time` values from these instances -- never a hardcoded
cadence string -- the same module-level-singleton pattern this codebase
already uses for `src.gateway.state.get_state()` and
`src.agents.llm_router.get_llm_router()`.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

_SCHEDULERS: dict[str, AsyncIOScheduler] = {}


def register_scheduler(name: str, scheduler: AsyncIOScheduler) -> None:
    _SCHEDULERS[name] = scheduler


def unregister_all() -> None:
    _SCHEDULERS.clear()


def get_all_schedulers() -> dict[str, AsyncIOScheduler]:
    return dict(_SCHEDULERS)
