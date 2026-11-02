"""The functions behind every `tradingos-cli` command (Build Spec §6.3) —
and, in a later phase, the API routes the Mission Control Console's
Config tab calls. One implementation, two entry points: nothing here is
CLI-specific.

Every mutation follows the same shape: load + validate the config file as
it stands (refuse to mutate on top of an already-broken file), apply the
requested change to an in-memory copy, write it back atomically, then run
it through the exact same apply_config_text() validation-and-apply gate a
hand-edit or hot-reload goes through — so a CLI-driven change can never
bypass schema validation, and always produces a real, auditable
agent_config_versions row. (If an app is also running and watching this
file, its watcher will independently notice the same write and apply it
again — a harmless extra version row, not a correctness issue.)
"""

import asyncio
import json
import weakref
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.apply import ApplyResult, apply_config_text
from src.gateway.edits import AgentSettingsPatch, apply_agent_settings, apply_provider_order
from src.gateway.loader import (
    ConfigLoadError,
    EffectiveAgentConfig,
    compute_effective_agents,
    load_and_validate,
    parse_config_text,
    validate_config_dict,
)
from src.gateway.roster import ROSTER_BY_ID
from src.gateway.schema import LlmProvider
from src.gateway.state import get_state
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus


class ServiceError(Exception):
    """A well-formed request the service layer can't fulfil (unknown agent
    id, rollback target that doesn't exist) — distinct from ConfigLoadError,
    which means the config file itself is unreadable or invalid.
    """


class StaleConfigError(Exception):
    """The config changed after the person read it: they edited from version `expected`, and
    `current` is what is running now. Nothing was written."""

    def __init__(self, expected: int, current: int | None) -> None:
        super().__init__(
            f"The configuration changed since you read it (you edited version {expected}, "
            f"version {current} is running now). Reload and make your change again."
        )
        self.expected = expected
        self.current = current


# One writer at a time, so two requests that both read version N cannot both be applied: the second
# finds N has moved on. A lock belongs to the event loop that uses it, so there is one per loop (a
# process has one; each test has its own).
_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _config_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


def _check_version(expected: int | None) -> None:
    """`expected` is the version the editor read; None means a caller (the CLI, which has no
    running app to disagree with) that does not ask."""
    if expected is None:
        return
    current = get_state().get_version_id()
    if current != expected:
        raise StaleConfigError(expected, current)


def _atomic_write(path: Path, text: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


async def _write_and_apply(
    db: AsyncSession, config_path: Path, text: str, *, source: str, actor: str | None = None
) -> ApplyResult:
    """Applies `text`, and writes it to the file only if it is a config the app will run on. A text
    that fails validation is recorded as a rejected version (and audited) and the file is left as it
    was, so a refused edit cannot leave a broken file on disk."""
    try:
        validate_config_dict(parse_config_text(text), raw_text=text)
    except ConfigLoadError:
        return await apply_config_text(db, text, source=source, actor=actor)
    # The watcher will see this write; it must know the apply below is the one that counts.
    get_state().note_self_write(text)
    _atomic_write(config_path, text)
    return await apply_config_text(db, text, source=source, actor=actor)


def _require_roster_agent(agent_id: str) -> None:
    if agent_id not in ROSTER_BY_ID:
        raise ServiceError(f"{agent_id!r} is not in the fixed 24-agent roster")


async def _mutate_and_apply(
    db: AsyncSession,
    config_path: Path,
    mutate_fn: Callable[[dict[str, Any]], None],
    *,
    source: str,
    actor: str | None = None,
    expected_version: int | None = None,
) -> ApplyResult:
    async with _config_lock():
        _check_version(expected_version)
        config = load_and_validate(config_path)  # raises ConfigLoadError if already broken
        data = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        mutate_fn(data)
        new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        return await _write_and_apply(db, config_path, new_text, source=source, actor=actor)


async def put_config(
    db: AsyncSession,
    config_path: Path,
    raw_text: str,
    *,
    source: str,
    actor: str | None = None,
    expected_version: int | None = None,
) -> ApplyResult:
    """Replaces the whole config with `raw_text` (the raw editor), if the person edited the version
    that is running."""
    async with _config_lock():
        _check_version(expected_version)
        return await _write_and_apply(db, config_path, raw_text, source=source, actor=actor)


def list_agents(config_path: Path) -> list[EffectiveAgentConfig]:
    """Every one of the 24 fixed agents' effective config, sorted by
    department then agent id. Reads and validates the file fresh — no DB
    involved, works whether or not any app process is currently running.
    """
    config = load_and_validate(config_path)
    effective = compute_effective_agents(config)
    return sorted(effective.values(), key=lambda a: (a.department, a.agent_id))


async def set_identity(
    db: AsyncSession,
    config_path: Path,
    agent_id: str,
    *,
    name: str | None = None,
    emoji: str | None = None,
    avatar: str | None = None,
    theme: str | None = None,
    voice: str | None = None,
    source: str = "cli",
    actor: str | None = None,
    expected_version: int | None = None,
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict[str, Any]) -> None:
        entries = data.setdefault("agents", {}).setdefault("entries", {})
        entry = entries.setdefault(agent_id, {})
        identity = entry.get("identity") or {"name": ROSTER_BY_ID[agent_id].display_name}
        if name is not None:
            identity["name"] = name
        if emoji is not None:
            identity["emoji"] = emoji
        if avatar is not None:
            identity["avatar"] = avatar
        if theme is not None:
            identity["theme"] = theme
        if voice is not None:
            identity["voice"] = voice
        entry["identity"] = identity

    return await _mutate_and_apply(
        db, config_path, mutate, source=source, actor=actor, expected_version=expected_version
    )


async def update_agent_settings(
    db: AsyncSession,
    config_path: Path,
    agent_id: str,
    patch: AgentSettingsPatch,
    *,
    known_skills: Collection[str],
    source: str,
    actor: str | None = None,
    expected_version: int,
) -> tuple[ApplyResult, list[str]]:
    """Changes one agent's settings in place and says which ones changed. Raises the refusals of
    `src.gateway.edits` (unknown agent, invalid edit, nothing to change) and `StaleConfigError`."""
    changed: list[str] = []

    def mutate(data: dict[str, Any]) -> None:
        changed.extend(apply_agent_settings(data, agent_id, patch, known_skills=known_skills))

    result = await _mutate_and_apply(
        db, config_path, mutate, source=source, actor=actor, expected_version=expected_version
    )
    return result, changed


async def update_provider_order(
    db: AsyncSession,
    config_path: Path,
    order: list[str],
    *,
    source: str,
    actor: str | None = None,
    expected_version: int,
) -> ApplyResult:
    """Replaces the LLM fallback order. Raises the refusals of `src.gateway.edits` and
    `StaleConfigError`."""

    def mutate(data: dict[str, Any]) -> None:
        apply_provider_order(data, order, known_providers={p.value for p in LlmProvider})

    return await _mutate_and_apply(
        db, config_path, mutate, source=source, actor=actor, expected_version=expected_version
    )


async def bind_agent(
    db: AsyncSession, config_path: Path, agent_id: str, channel: str, account_id: str | None = None
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict[str, Any]) -> None:
        bindings = data.setdefault("bindings", [])
        match: dict[str, Any] = {"channel": channel}
        if account_id is not None:
            match["accountId"] = account_id
        bindings.append({"agentId": agent_id, "match": match})

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


async def unbind_agent(
    db: AsyncSession, config_path: Path, agent_id: str, channel: str, account_id: str | None = None
) -> ApplyResult:
    def mutate(data: dict[str, Any]) -> None:
        bindings = data.get("bindings", [])
        data["bindings"] = [
            b
            for b in bindings
            if not (
                b["agentId"] == agent_id
                and b["match"]["channel"] == channel
                and b["match"].get("accountId") == account_id
            )
        ]

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


def _current_skills(data: dict[str, Any], agent_id: str) -> list[str]:
    entry = data.get("agents", {}).get("entries", {}).get(agent_id, {})
    skills = entry.get("skills")
    if skills is not None:
        return list(skills)
    return list(data.get("agents", {}).get("defaults", {}).get("skills", []))


async def grant_skill(
    db: AsyncSession, config_path: Path, agent_id: str, skill: str
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict[str, Any]) -> None:
        entries = data.setdefault("agents", {}).setdefault("entries", {})
        entry = entries.setdefault(agent_id, {})
        skills = _current_skills(data, agent_id)
        if skill not in skills:
            skills.append(skill)
        entry["skills"] = skills

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


async def revoke_skill(
    db: AsyncSession, config_path: Path, agent_id: str, skill: str
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict[str, Any]) -> None:
        entries = data.setdefault("agents", {}).setdefault("entries", {})
        entry = entries.setdefault(agent_id, {})
        entry["skills"] = [s for s in _current_skills(data, agent_id) if s != skill]

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


async def set_heartbeat(
    db: AsyncSession,
    config_path: Path,
    agent_id: str,
    *,
    enabled: bool,
    interval_minutes: int | None = None,
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict[str, Any]) -> None:
        entries = data.setdefault("agents", {}).setdefault("entries", {})
        entry = entries.setdefault(agent_id, {})
        entry["heartbeatEnabled"] = enabled
        if interval_minutes is not None:
            entry["heartbeatIntervalMinutes"] = interval_minutes
        elif not enabled:
            entry.pop("heartbeatIntervalMinutes", None)

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    errors: list[str]


def validate_config(config_path: Path) -> ValidationResult:
    """Dry-run schema validation — no DB, no write, nothing applied."""
    try:
        load_and_validate(config_path)
    except ConfigLoadError as exc:
        return ValidationResult(ok=False, errors=exc.errors)
    return ValidationResult(ok=True, errors=[])


async def rollback_config(
    db: AsyncSession,
    config_path: Path,
    to_version: int,
    *,
    source: str = "cli-rollback",
    actor: str | None = None,
) -> ApplyResult:
    row = await db.get(AgentConfigVersion, to_version)
    if row is None:
        raise ServiceError(f"No config version {to_version} exists")
    if row.status != ConfigVersionStatus.ACTIVE:
        raise ServiceError(
            f"Config version {to_version} was rejected, not active — nothing to roll back to"
        )

    async with _config_lock():
        return await _write_and_apply(db, config_path, row.raw_content, source=source, actor=actor)


@dataclass(frozen=True, slots=True)
class DoctorIssue:
    description: str
    fixed: bool


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    load_errors: list[str]
    issues: list[DoctorIssue]
    apply_result: ApplyResult | None


async def doctor(db: AsyncSession, config_path: Path, *, fix: bool) -> DoctorReport:
    """Detects known-safe, mechanically-fixable config issues: exact
    duplicate bindings and exact duplicate agentToAgentPolicy.allow rules.
    Reports them always; repairs them (writes + applies) only with fix=True.
    A file that doesn't even parse/validate is reported as such, not
    "fixed" — doctor doesn't guess at intent.
    """
    try:
        config = load_and_validate(config_path)
    except ConfigLoadError as exc:
        return DoctorReport(ok=False, load_errors=exc.errors, issues=[], apply_result=None)

    data = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    issues: list[DoctorIssue] = []

    seen_bindings: set[tuple[Any, ...]] = set()
    deduped_bindings = []
    for binding in data.get("bindings", []):
        key = (binding["agentId"], binding["match"]["channel"], binding["match"].get("accountId"))
        if key in seen_bindings:
            issues.append(DoctorIssue(f"duplicate binding: {key}", fixed=fix))
            continue
        seen_bindings.add(key)
        deduped_bindings.append(binding)

    seen_rules: set[tuple[Any, ...]] = set()
    deduped_allow = []
    for rule in data.get("agentToAgentPolicy", {}).get("allow", []):
        key = (rule["from"], rule["to"], rule["scope"])
        if key in seen_rules:
            issues.append(DoctorIssue(f"duplicate agentToAgentPolicy.allow rule: {key}", fixed=fix))
            continue
        seen_rules.add(key)
        deduped_allow.append(rule)

    apply_result: ApplyResult | None = None
    if fix and issues:
        data["bindings"] = deduped_bindings
        if "agentToAgentPolicy" in data:
            data["agentToAgentPolicy"]["allow"] = deduped_allow
        new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        _atomic_write(config_path, new_text)
        apply_result = await apply_config_text(db, new_text, source="cli-doctor")

    return DoctorReport(ok=True, load_errors=[], issues=issues, apply_result=apply_result)
