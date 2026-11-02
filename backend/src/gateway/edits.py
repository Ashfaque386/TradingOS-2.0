"""What a console edit does to the gateway config: one agent's settings, or the LLM provider order.

The console used to read the whole config, change a piece of it in the browser and write the whole
thing back, so two people editing at once overwrote each other and every save rewrote the file from
what the page had last seen. These functions change a piece of the config's data in place on the
server, under the version the person read (see `src.gateway.service`), and say what changed. They
touch nothing but data: reading the file, validating it, writing and applying it are the service's.

An edit that would change nothing is refused (`NothingToChange`), so a repeated request does not
write a new version and an audit row for something that did not happen.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from src.gateway.roster import ROSTER_BY_ID


class EditRefused(ValueError):
    """The edit cannot be made; the message says why, for the person who asked."""


class UnknownAgent(EditRefused):
    """The agent is not one of the fixed roster's."""


class InvalidEdit(EditRefused):
    """The edit asks for something the config does not allow."""


class NothingToChange(EditRefused):
    """Everything asked for is already so."""


@dataclass(frozen=True, slots=True)
class AgentSettingsPatch:
    """What to change about one agent; a field left None is left alone."""

    enabled: bool | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_interval_minutes: int | None = None
    skills: tuple[str, ...] | None = None


def _entry(data: dict[str, Any], agent_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = data.get("agents", {}).get("entries", {}).get(agent_id, {})
    return entry


def effective_settings(data: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """The agent's settings as the app resolves them: its own entry over the defaults."""
    defaults = data.get("agents", {}).get("defaults", {})
    entry = _entry(data, agent_id)
    enabled = entry.get("enabled")
    heartbeat = entry.get("heartbeatEnabled")
    skills = entry.get("skills")
    return {
        "enabled": True if enabled is None else enabled,
        "heartbeat_enabled": defaults.get("heartbeatEnabled", False)
        if heartbeat is None
        else heartbeat,
        "heartbeat_interval_minutes": entry.get("heartbeatIntervalMinutes"),
        "skills": sorted(defaults.get("skills", []) if skills is None else skills),
    }


def apply_agent_settings(
    data: dict[str, Any],
    agent_id: str,
    patch: AgentSettingsPatch,
    *,
    known_skills: Collection[str],
) -> list[str]:
    """Changes `data` in place and returns the names of the settings that changed.

    Raises `UnknownAgent`, `InvalidEdit` (an unknown skill, a protected agent switched off, a
    heartbeat interval that is not positive) or `NothingToChange`."""
    roster_agent = ROSTER_BY_ID.get(agent_id)
    if roster_agent is None:
        raise UnknownAgent(f"{agent_id!r} is not in the fixed agent roster")
    if patch == AgentSettingsPatch():
        raise NothingToChange("Nothing was asked to change.")
    if patch.enabled is False and not roster_agent.can_disable:
        raise InvalidEdit(
            f"{roster_agent.display_name} cannot be switched off: it is required for the "
            "governance and safety of the trading system."
        )
    if patch.heartbeat_interval_minutes is not None and patch.heartbeat_interval_minutes < 1:
        raise InvalidEdit("The heartbeat interval must be at least one minute.")
    if patch.skills is not None:
        unknown = sorted(set(patch.skills) - set(known_skills))
        if unknown:
            raise InvalidEdit(
                f"No such skill: {', '.join(unknown)}. Skills are the fixed set the app ships with."
            )

    before = effective_settings(data, agent_id)
    entry = data.setdefault("agents", {}).setdefault("entries", {}).setdefault(agent_id, {})
    if patch.enabled is not None:
        entry["enabled"] = patch.enabled
    if patch.heartbeat_enabled is not None:
        entry["heartbeatEnabled"] = patch.heartbeat_enabled
        if not patch.heartbeat_enabled and patch.heartbeat_interval_minutes is None:
            entry.pop("heartbeatIntervalMinutes", None)
    if patch.heartbeat_interval_minutes is not None:
        entry["heartbeatIntervalMinutes"] = patch.heartbeat_interval_minutes
    if patch.skills is not None:
        entry["skills"] = list(dict.fromkeys(patch.skills))
    after = effective_settings(data, agent_id)

    changed = [name for name in before if before[name] != after[name]]
    if not changed:
        raise NothingToChange("Everything asked for is already so.")
    return changed


def apply_provider_order(
    data: dict[str, Any], order: list[str], *, known_providers: Collection[str]
) -> None:
    """Replaces `infra.llmProviders.order`. Raises `InvalidEdit` for an empty order, a provider the
    app does not know, or one listed twice, and `NothingToChange` for the order already in place."""
    if not order:
        raise InvalidEdit("The fallback chain needs at least one provider.")
    unknown = sorted(set(order) - set(known_providers))
    if unknown:
        raise InvalidEdit(f"No such LLM provider: {', '.join(unknown)}.")
    if len(set(order)) != len(order):
        raise InvalidEdit("A provider can be in the fallback chain only once.")
    providers = data.setdefault("infra", {}).setdefault("llmProviders", {})
    if providers.get("order") == order:
        raise NothingToChange("The fallback chain is already in that order.")
    providers["order"] = list(order)
