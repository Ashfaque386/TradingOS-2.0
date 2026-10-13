"""E2E test-fixture seeding: a LangGraph pipeline run whose process died.

Why a script: a run that is killed mid-step cannot be produced through the UI or
API -- the pipeline executes inside the HTTP request that started it, so the only
way to leave one behind is to kill the backend at the right moment. e2e/
pipeline-abandoned.spec.ts needs both halves of what happens next, so this plants
the run directly in the database, with the same events (and payload shapes) a real
run writes up to the point it "died":

    pipeline.started -> step.started/step.completed (ceo_kickoff) -> step.started
    (market_analysis_step) -> ...nothing.

Two modes:

- default: the run just stops, its last event ten minutes old. Seeded *before* the
  backend starts (see the e2e job in ci.yml), so the backend's startup sweep is
  what ends it -- the spec then checks the `pipeline.abandoned` event is there.
- `--abandoned`: the same run plus the `pipeline.abandoned` event the sweep would
  have written, stamped now. Seeded at test time, so the Mission Control panel has
  an interrupted run as its most recent one to render.

Re-seeding the same --run-id replaces it, so the suite can be re-run against one
database.

Usage:
    python scripts/seed_e2e_pipeline_run.py --run-id <uuid> --objective "..." [--abandoned]
"""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from src.core.db import get_session_factory
from src.models.agent_pipeline_event import AgentPipelineEvent

_DEAD_FOR = timedelta(minutes=10)


_Event = tuple[str, str | None, dict]


def _events(objective: str, *, abandoned: bool, now: datetime) -> list[_Event]:
    events: list[_Event] = [
        ("pipeline.started", None, {"objective": objective}),
        ("step.started", "ceo_kickoff", {"step_index": 0}),
        (
            "step.completed",
            "ceo_kickoff",
            {"step_index": 0, "duration_ms": 12.0, "output": {"ceo_brief": {"source": "fallback"}}},
        ),
        ("step.started", "market_analysis_step", {"step_index": 1}),
    ]
    if abandoned:
        events.append(
            (
                "pipeline.abandoned",
                "market_analysis_step",
                {
                    "reason": "the process running this pipeline ended before the run finished",
                    "last_event": "step.started",
                    "last_event_at": (now - _DEAD_FOR).isoformat(),
                    "steps_completed": 1,
                },
            )
        )
    return events


async def seed(run_id: uuid.UUID, objective: str, *, abandoned: bool) -> None:
    now = datetime.now(UTC)
    events = _events(objective, abandoned=abandoned, now=now)
    # The run's own events end ten minutes ago; the reaper's ending, if present, is now.
    last_of_run = len(events) - 1 if not abandoned else len(events) - 2

    async with get_session_factory()() as db:
        await db.execute(delete(AgentPipelineEvent).where(AgentPipelineEvent.run_id == run_id))
        for i, (event_type, node, payload) in enumerate(events):
            if event_type == "pipeline.abandoned":
                created_at = now
            else:
                created_at = now - _DEAD_FOR - timedelta(seconds=last_of_run - i)
            db.add(
                AgentPipelineEvent(
                    run_id=run_id,
                    sequence=i + 1,
                    event_type=event_type,
                    node=node,
                    agent_id=None,
                    payload=payload,
                    created_at=created_at,
                )
            )
        await db.commit()
    print(run_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", type=uuid.UUID, required=True)
    parser.add_argument("--objective", default="E2E interrupted pipeline run")
    parser.add_argument("--abandoned", action="store_true")
    args = parser.parse_args()
    asyncio.run(seed(args.run_id, args.objective, abandoned=args.abandoned))
