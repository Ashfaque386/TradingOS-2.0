"""Shared types for the Skill Registry (Build Spec §16)."""

from collections.abc import Awaitable, Callable

# Phase 12 (Build Spec §18): notification-send genuinely needs real
# network I/O, so a skill function may now be sync (every other entry --
# deterministic/stub, no I/O) or async (notification-send alone) --
# src.agents.tools.registry.execute_skill awaits the result only when
# it's awaitable, so every existing sync skill is unaffected.
SkillFn = Callable[[dict], "dict | Awaitable[dict]"]


class SkillError(Exception):
    """Base for every Skill Registry failure."""


class SkillNotFoundError(SkillError):
    """Raised for a skill name that isn't one of the in-repo SKILLS entries.

    There is deliberately no code path that can register a skill not
    already present in src.agents.tools.registry.SKILLS at import time
    (Build Spec §16: "No external ingestion path") -- this exception is
    what a lookup miss looks like, not a signal that dynamic loading almost
    worked.
    """


class SkillNotGrantedError(SkillError):
    """Raised when the calling agent's effective config (Gateway-managed,
    global defaults + per-agent overrides) does not include this skill in
    its granted skills list.
    """
