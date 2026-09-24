"""Reads config/tradingos.config.json (JSON5-tolerant), validates it
against the schema, and computes the effective (defaults + per-agent
overrides) config for every agent in the fixed roster.
"""

from dataclasses import dataclass
from pathlib import Path

import json5
from pydantic import ValidationError

from src.gateway.roster import ROSTER
from src.gateway.schema import AgentEntryConfig, TradingOSConfig


class ConfigLoadError(Exception):
    """Raised for any failure to produce a valid TradingOSConfig: the file
    is missing, isn't valid JSON5, or fails schema validation. Carries the
    raw text (when available) and a human-readable list of error messages
    so callers can persist both for the config version history.
    """

    def __init__(self, message: str, *, raw_text: str | None, errors: list[str]) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.errors = errors


def read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(
            f"Could not read config file {path}: {exc}", raw_text=None, errors=[str(exc)]
        ) from exc


def parse_config_text(text: str) -> dict:
    try:
        data = json5.loads(text)
    except ValueError as exc:
        raise ConfigLoadError(
            f"Config file is not valid JSON5: {exc}", raw_text=text, errors=[str(exc)]
        ) from exc

    if not isinstance(data, dict):
        raise ConfigLoadError(
            "Config file must parse to a JSON object at the top level",
            raw_text=text,
            errors=["top-level value is not an object"],
        )
    try:
        return _join_surrogate_pairs(data)
    except UnicodeDecodeError as exc:
        raise ConfigLoadError(
            "Config contains an unpaired UTF-16 surrogate escape",
            raw_text=text,
            errors=[f"invalid \\u escape sequence: {exc.reason}"],
        ) from exc


def _join_surrogate_pairs(value):
    """json5 decodes an escaped astral character (e.g. "\\ud83e\\udde0", which
    is how Python's json.dumps writes the agent emoji) into two lone
    surrogates instead of one character, unlike the stdlib json module.
    Those can't be encoded as UTF-8, so storing the config crashed with a
    500. Rejoin every pair; a truly unpaired one raises UnicodeDecodeError."""
    if isinstance(value, str):
        if any("\ud800" <= ch <= "\udfff" for ch in value):
            return value.encode("utf-16", "surrogatepass").decode("utf-16")
        return value
    if isinstance(value, list):
        return [_join_surrogate_pairs(v) for v in value]
    if isinstance(value, dict):
        return {_join_surrogate_pairs(k): _join_surrogate_pairs(v) for k, v in value.items()}
    return value


def validate_config_dict(data: dict, *, raw_text: str | None = None) -> TradingOSConfig:
    try:
        return TradingOSConfig.model_validate(data)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        raise ConfigLoadError(
            f"Config failed schema validation ({len(errors)} error(s))",
            raw_text=raw_text,
            errors=errors,
        ) from exc


def load_and_validate(path: Path) -> TradingOSConfig:
    """Read + parse + validate in one call. Raises ConfigLoadError on any
    failure at any stage.
    """
    text = read_config_text(path)
    data = parse_config_text(text)
    return validate_config_dict(data, raw_text=text)


@dataclass(frozen=True, slots=True)
class EffectiveAgentConfig:
    agent_id: str
    department: str
    identity_name: str
    emoji: str | None
    avatar: str | None
    theme: str | None
    voice: str | None
    model: str
    enabled: bool
    heartbeat_enabled: bool
    heartbeat_interval_minutes: int | None
    skills: tuple[str, ...]


def _merge_one(
    agent_id: str, department: str, display_name: str, override: AgentEntryConfig | None, defaults
) -> EffectiveAgentConfig:
    identity = override.identity if override else None
    return EffectiveAgentConfig(
        agent_id=agent_id,
        department=department,
        identity_name=identity.name if identity else display_name,
        emoji=identity.emoji if identity else None,
        avatar=identity.avatar if identity else None,
        theme=identity.theme if identity else None,
        voice=identity.voice if identity else None,
        model=(override.model if override and override.model is not None else defaults.model),
        enabled=(override.enabled if override and override.enabled is not None else True),
        heartbeat_enabled=(
            override.heartbeat_enabled
            if override and override.heartbeat_enabled is not None
            else defaults.heartbeat_enabled
        ),
        heartbeat_interval_minutes=(override.heartbeat_interval_minutes if override else None),
        skills=tuple(
            override.skills if override and override.skills is not None else defaults.skills
        ),
    )


def compute_effective_agents(config: TradingOSConfig) -> dict[str, EffectiveAgentConfig]:
    """The merged view of every one of the 24 fixed agents: roster identity
    defaults, overridden by config.agents.defaults, overridden by that
    agent's own config.agents.entries[agent_id] if present.
    """
    defaults = config.agents.defaults
    result: dict[str, EffectiveAgentConfig] = {}
    for roster_agent in ROSTER:
        override = config.agents.entries.get(roster_agent.agent_id)
        result[roster_agent.agent_id] = _merge_one(
            roster_agent.agent_id,
            roster_agent.department.value,
            roster_agent.display_name,
            override,
            defaults,
        )
    return result
