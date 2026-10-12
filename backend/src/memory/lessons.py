"""What agent memory may learn from, and how a lesson reaches a prompt.

The rule this module exists to enforce: **a lesson is recorded only if the
system can vouch for the fact behind it.** Memory is replayed into later LLM
prompts as "lessons from earlier similar runs", and an LLM cannot tell a real
outcome from a simulated one -- so a false lesson is not noise, it is
misinformation with authority.

That rule was broken once. The LangGraph pipeline's backtesting, evaluation and
deployment nodes are deterministic stand-ins (a hard-coded Sharpe of 1.0, a
`deployed: True` that deploys nothing), and the first version of memory recorded
their output as "evaluated pass (sharpe 1.0)... deployed to paper". Two things
now stand between a stand-in and a lesson:

- stand-in nodes mark their output `simulated: true` (`is_simulated` reads it),
  so their results are visible as such in the feed and the API response and can
  be refused here; and
- the only lessons the pipelines record are **failures of the generated code
  itself** -- it did not pass static validation, or its sandbox smoke run
  failed -- because those depend on the code alone. Nothing derived from market
  data is recorded (Sharpe, P&L, review findings), because nothing records
  whether a backtest or a paper fill ran on real or synthetic data, and a
  lesson built on synthetic data would teach the same falsehood by a quieter
  route. Also deliberately not recorded: the naked-options scan blocking a
  strategy (the legs are fixed, not written by the model, and grounding them
  depends on chain data being present) and an infrastructure fault in the
  sandbox (a crashed worker says nothing about the code). When results carry
  provenance, that is the point to add performance lessons.

This module imports nothing heavy on purpose: the LangGraph graph and the
strategy orchestration both use `AgentMemory` as a Protocol, so neither needs
Redis, Qdrant or an embedding provider.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

SIMULATED_KEY = "simulated"

# Longest an individual piece of text (an error message, an objective) may be
# once it is quoted inside a lesson -- a lesson is a pointer, not a log.
MAX_QUOTED_CHARS = 160
MAX_QUOTED_ERRORS = 3

# Memory is an aid to a run, never a reason for one to stall. The embedding
# client waits up to a minute on a host that never answers (the vector-store
# client ten seconds, per call); a run (a `POST /strategies` request, say) gives
# memory this long instead.
MEMORY_CALL_TIMEOUT_SECONDS = 10.0


class AgentMemory(Protocol):
    """Agent long-term memory as a pipeline sees it. Implementations must not
    raise -- memory only ever adds context to a run, it never fails one --
    and callers go through `recall_lessons` / `record_lesson` below, which
    guard against one that does anyway."""

    async def recall(self, objective: str) -> list[str]: ...

    async def remember(self, text: str, metadata: dict[str, Any]) -> None: ...


def is_simulated(value: Any) -> bool:
    """True for a result that a stand-in produced (or that does not exist):
    nothing real to learn from. A dict is simulated only if it says so."""
    if value is None:
        return True
    return isinstance(value, dict) and value.get(SIMULATED_KEY) is True


def quote(text: str, limit: int = MAX_QUOTED_CHARS) -> str:
    """One line, bounded -- for putting an objective or an error message inside
    a lesson."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def quote_errors(errors: list[str] | None) -> str:
    """The first few errors, each bounded; '' if there are none."""
    shown = [quote(e) for e in (errors or [])[:MAX_QUOTED_ERRORS]]
    more = len(errors or []) - len(shown)
    return "; ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def format_lessons_block(lessons: list[str]) -> str:
    """Text to append to a prompt, after the task so the task's own framing
    still leads. Empty when there is nothing to say, so a prompt with no
    recalled lessons is exactly what it was before memory existed."""
    if not lessons:
        return ""
    bullets = "\n".join(f"- {lesson}" for lesson in lessons)
    return f"\n\nLessons from earlier similar runs (use if relevant):\n{bullets}"


async def recall_lessons(memory: AgentMemory | None, objective: str) -> list[str]:
    """Lessons for an objective, or [] -- never raises, never waits longer than
    `MEMORY_CALL_TIMEOUT_SECONDS`."""
    if memory is None:
        return []
    try:
        return await asyncio.wait_for(memory.recall(objective), MEMORY_CALL_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - memory must never break the run it serves
        logger.warning("memory.recall_failed", error=type(exc).__name__)
        return []


async def record_lesson(memory: AgentMemory | None, text: str, metadata: dict[str, Any]) -> None:
    """Hand a lesson to memory -- never raises, never waits longer than
    `MEMORY_CALL_TIMEOUT_SECONDS`."""
    if memory is None:
        return
    try:
        await asyncio.wait_for(memory.remember(text, metadata), MEMORY_CALL_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - memory must never break the run it serves
        logger.warning("memory.remember_failed", error=type(exc).__name__)
