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

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.apply import ApplyResult, apply_config_text
from src.gateway.loader import ConfigLoadError, compute_effective_agents, load_and_validate
from src.gateway.roster import ROSTER_BY_ID
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus


class ServiceError(Exception):
    """A well-formed request the service layer can't fulfil (unknown agent
    id, rollback target that doesn't exist) — distinct from ConfigLoadError,
    which means the config file itself is unreadable or invalid.
    """


def _atomic_write(path: Path, text: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _require_roster_agent(agent_id: str) -> None:
    if agent_id not in ROSTER_BY_ID:
        raise ServiceError(f"{agent_id!r} is not in the fixed 24-agent roster")


async def _mutate_and_apply(
    db: AsyncSession, config_path: Path, mutate_fn: Callable[[dict], None], *, source: str
) -> ApplyResult:
    config = load_and_validate(config_path)  # raises ConfigLoadError if already broken
    data = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    mutate_fn(data)
    new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(config_path, new_text)
    return await apply_config_text(db, new_text, source=source)


def list_agents(config_path: Path):
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
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict) -> None:
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

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


async def bind_agent(
    db: AsyncSession, config_path: Path, agent_id: str, channel: str, account_id: str | None = None
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict) -> None:
        bindings = data.setdefault("bindings", [])
        match: dict = {"channel": channel}
        if account_id is not None:
            match["accountId"] = account_id
        bindings.append({"agentId": agent_id, "match": match})

    return await _mutate_and_apply(db, config_path, mutate, source="cli")


async def unbind_agent(
    db: AsyncSession, config_path: Path, agent_id: str, channel: str, account_id: str | None = None
) -> ApplyResult:
    def mutate(data: dict) -> None:
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


def _current_skills(data: dict, agent_id: str) -> list[str]:
    entry = data.get("agents", {}).get("entries", {}).get(agent_id, {})
    skills = entry.get("skills")
    if skills is not None:
        return list(skills)
    return list(data.get("agents", {}).get("defaults", {}).get("skills", []))


async def grant_skill(
    db: AsyncSession, config_path: Path, agent_id: str, skill: str
) -> ApplyResult:
    _require_roster_agent(agent_id)

    def mutate(data: dict) -> None:
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

    def mutate(data: dict) -> None:
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

    def mutate(data: dict) -> None:
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


async def rollback_config(db: AsyncSession, config_path: Path, to_version: int) -> ApplyResult:
    row = await db.get(AgentConfigVersion, to_version)
    if row is None:
        raise ServiceError(f"No config version {to_version} exists")
    if row.status != ConfigVersionStatus.ACTIVE:
        raise ServiceError(
            f"Config version {to_version} was rejected, not active — nothing to roll back to"
        )

    _atomic_write(config_path, row.raw_content)
    return await apply_config_text(db, row.raw_content, source="cli-rollback")


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

    seen_bindings: set[tuple] = set()
    deduped_bindings = []
    for binding in data.get("bindings", []):
        key = (binding["agentId"], binding["match"]["channel"], binding["match"].get("accountId"))
        if key in seen_bindings:
            issues.append(DoctorIssue(f"duplicate binding: {key}", fixed=fix))
            continue
        seen_bindings.add(key)
        deduped_bindings.append(binding)

    seen_rules: set[tuple] = set()
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
