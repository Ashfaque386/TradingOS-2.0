"""The skills the app ships with, described for the console.

`src.agents.tools.registry.SKILLS` is the fixed set an agent can be granted: there is no way to add
one at run time. The Settings page and the Agent Fleet used to ask for a skill's id as free text, so
a typo granted a skill that does not exist and did nothing. This says what each one is, whether it
does anything yet, and who holds it; the registry's own keys are the authority on which exist (a
test holds this module to them).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from src.gateway.loader import EffectiveAgentConfig


@dataclass(frozen=True, slots=True)
class SkillInfo:
    skill_id: str
    summary: str
    # "read" only looks; "action" does something outside the app (sends a message).
    effect: Literal["read", "action"]
    # False: a placeholder with the real interface that returns an empty result, because the thing
    # it would read is not wired yet. Granting it is harmless and changes nothing.
    working: bool


SKILL_INFO: dict[str, SkillInfo] = {
    info.skill_id: info
    for info in (
        SkillInfo(
            "market-data-read",
            "Reads the latest price of a share. A placeholder for now: it returns no price, "
            "because no live feed is wired to it.",
            "read",
            False,
        ),
        SkillInfo(
            "option-chain-read",
            "Reads an option chain for an underlying. A placeholder for now: it returns no legs, "
            "because no live feed is wired to it.",
            "read",
            False,
        ),
        SkillInfo(
            "portfolio-status-read",
            "Reads positions and cash. A placeholder for now: it returns none, because no "
            "portfolio store is wired to it.",
            "read",
            False,
        ),
        SkillInfo(
            "code-format-lint",
            "Checks that Python code parses and imports nothing from the banned list "
            "(os, subprocess, socket, sys, shutil, ctypes).",
            "read",
            True,
        ),
        SkillInfo(
            "sandbox-dry-run",
            "Runs code in the strategy sandbox. A placeholder for now: it reports a clean run "
            "without running anything.",
            "read",
            False,
        ),
        SkillInfo(
            "notification-send",
            "Sends one message to a named channel (Telegram, Discord or Slack) using the "
            "settings that channel already has.",
            "action",
            True,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    info: SkillInfo
    granted_to: tuple[str, ...]


def build_catalog(agents: Mapping[str, EffectiveAgentConfig]) -> list[SkillCatalogEntry]:
    """Every shipped skill, with the agents that hold it now (defaults included), by skill id."""
    return [
        SkillCatalogEntry(
            info=info,
            granted_to=tuple(
                sorted(a.agent_id for a in agents.values() if info.skill_id in a.skills)
            ),
        )
        for info in sorted(SKILL_INFO.values(), key=lambda i: i.skill_id)
    ]
