"""A plain-language account of what one agent is doing, from what the app really recorded.

The old console had a per-agent "what is this agent doing" sentence. This builds one the same way
every figure here is built: from rows that exist, never from a model's guess. It reads the agent's
settings, its heartbeat self-checks and the tasks that claimed its capabilities, and says what they
show, including what is missing (no data source, no heartbeat, no tasks) instead of staying silent
about it. The composing is pure; the route gathers the facts.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LineKind = Literal["state", "heartbeat", "work", "gap"]


@dataclass(frozen=True, slots=True)
class HeartbeatFact:
    status: str  # "ok" or "alert"
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class TaskFact:
    name: str
    status: str  # pending, running, succeeded, failed, ...
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NarrationFacts:
    display_name: str
    enabled: bool
    can_disable: bool
    heartbeat_enabled: bool
    skills: tuple[str, ...]
    has_data_source: bool
    capabilities: tuple[str, ...]
    heartbeats: Sequence[HeartbeatFact]  # newest first
    tasks: Sequence[TaskFact]  # newest first
    now: datetime


@dataclass(frozen=True, slots=True)
class NarrationLine:
    kind: LineKind
    text: str


def ago(now: datetime, then: datetime) -> str:
    """`just now`, `5 minutes ago`, `3 hours ago`, `2 days ago`."""
    seconds = max(0, int((now - then).total_seconds()))
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


# A task is still going if it has not finished and not been stopped.
_ACTIVE = frozenset({"pending", "ready", "claimed", "running"})

# How the newest task is described, by its status.
_PHRASE = {
    "succeeded": "succeeded",
    "failed": "failed",
    "running": "is running",
    "claimed": "is running",
    "ready": "is waiting to be picked up",
    "pending": "is waiting on other tasks",
    "unsatisfiable": "could not run, because a task it depends on failed",
    "cancelled": "was cancelled with its run",
}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def narrate(facts: NarrationFacts) -> list[NarrationLine]:
    name = facts.display_name
    lines: list[NarrationLine] = []

    if facts.enabled:
        lines.append(NarrationLine("state", f"{name} is switched on."))
    else:
        lines.append(NarrationLine("state", f"{name} is switched off, so it takes no work."))
    if not facts.can_disable:
        lines.append(
            NarrationLine("state", "It cannot be switched off: governance and safety depend on it.")
        )
    if facts.skills:
        skills = ", ".join(sorted(facts.skills))
        lines.append(
            NarrationLine("state", f"It may use {_plural(len(facts.skills), 'skill')}: {skills}.")
        )
    else:
        lines.append(NarrationLine("state", "It has been granted no skills."))

    if not facts.has_data_source:
        lines.append(
            NarrationLine(
                "gap",
                "Its subject matter has no real data source in this system yet, so it reports "
                "nothing rather than a made-up figure.",
            )
        )

    if not facts.heartbeat_enabled:
        lines.append(
            NarrationLine("heartbeat", "Its heartbeat check is off, so it records no self-checks.")
        )
    elif not facts.heartbeats:
        lines.append(
            NarrationLine("heartbeat", "Its heartbeat check is on, but none has been recorded yet.")
        )
    else:
        latest = facts.heartbeats[0]
        lines.append(
            NarrationLine(
                "heartbeat",
                f"Its last self-check was {ago(facts.now, latest.checked_at)} and found "
                f"{'an alert' if latest.status == 'alert' else 'nothing wrong'}.",
            )
        )
        alerts = sum(1 for h in facts.heartbeats if h.status == "alert")
        if alerts:
            lines.append(
                NarrationLine(
                    "heartbeat",
                    f"{alerts} of its last {_plural(len(facts.heartbeats), 'self-check')} raised "
                    "an alert.",
                )
            )

    if not facts.capabilities:
        lines.append(
            NarrationLine(
                "work",
                "It claims no task capability, so the task engine records no work for it here.",
            )
        )
    elif not facts.tasks:
        lines.append(NarrationLine("work", "No task has claimed its capabilities yet."))
    else:
        succeeded = sum(1 for t in facts.tasks if t.status == "succeeded")
        failed = sum(1 for t in facts.tasks if t.status == "failed")
        active = sum(1 for t in facts.tasks if t.status in _ACTIVE)
        stopped = len(facts.tasks) - succeeded - failed - active
        still = "is" if active == 1 else "are"
        summary = (
            f"Of its last {_plural(len(facts.tasks), 'task')}, {succeeded} succeeded, "
            f"{failed} failed and {active} {still} still waiting or running"
        )
        if stopped:
            summary += f"; {stopped} {'was' if stopped == 1 else 'were'} stopped before finishing"
        lines.append(NarrationLine("work", summary + "."))
        newest = facts.tasks[0]
        phrase = _PHRASE.get(newest.status, f"has the status {newest.status}")
        lines.append(
            NarrationLine(
                "work",
                f"The newest, {newest.name!r}, {phrase} "
                f"(started {ago(facts.now, newest.created_at)}).",
            )
        )
        if newest.status == "failed" and newest.last_error:
            lines.append(NarrationLine("work", f"It failed with: {newest.last_error}"))

    return lines
