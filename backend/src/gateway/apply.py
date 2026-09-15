"""The one code path that turns a config file's contents into either an
active, applied Agent Gateway config or a rejected version row — used by
the hot-reload watcher, the CLI, and (in a later phase) the API. Never
raises for a bad config; only for a genuine infra failure (DB down).
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.loader import (
    ConfigLoadError,
    compute_effective_agents,
    parse_config_text,
    read_config_text,
    validate_config_dict,
)
from src.gateway.schema import TradingOSConfig
from src.gateway.state import get_state
from src.models.agent_binding import AgentBinding
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus
from src.models.agent_identity import AgentIdentity
from src.models.agent_to_agent_policy import AgentToAgentPolicy
from src.models.audit_log import AuditLog


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: ConfigVersionStatus
    version_id: int
    errors: list[str]


async def _sync_agent_identities(session: AsyncSession, config: TradingOSConfig) -> None:
    for agent_id, effective in compute_effective_agents(config).items():
        values = {
            "name": effective.identity_name,
            "emoji": effective.emoji,
            "avatar": effective.avatar,
            "theme": effective.theme,
            "voice": effective.voice,
        }
        stmt = (
            pg_insert(AgentIdentity)
            .values(agent_id=agent_id, **values)
            .on_conflict_do_update(index_elements=[AgentIdentity.agent_id], set_=values)
        )
        await session.execute(stmt)


async def _sync_agent_bindings(session: AsyncSession, config: TradingOSConfig) -> None:
    await session.execute(delete(AgentBinding))
    for binding in config.bindings:
        session.add(
            AgentBinding(
                agent_id=binding.agent_id,
                channel=binding.match.channel,
                account_id=binding.match.account_id,
            )
        )


async def _sync_agent_to_agent_policy(session: AsyncSession, config: TradingOSConfig) -> None:
    await session.execute(delete(AgentToAgentPolicy))
    for rule in config.agent_to_agent_policy.allow:
        session.add(
            AgentToAgentPolicy(from_agent=rule.from_agent, to_agent=rule.to_agent, scope=rule.scope)
        )


async def _record_active(
    session: AsyncSession, *, source: str, raw_text: str, config: TradingOSConfig
) -> ApplyResult:
    version = AgentConfigVersion(
        status=ConfigVersionStatus.ACTIVE,
        source=source,
        raw_content=raw_text,
        parsed_config=config.model_dump(mode="json", by_alias=True),
        validation_errors=None,
    )
    session.add(version)
    await session.flush()

    await _sync_agent_identities(session, config)
    await _sync_agent_bindings(session, config)
    await _sync_agent_to_agent_policy(session, config)

    session.add(
        AuditLog(
            actor=source,
            action="agent_gateway.config_applied",
            entity_type="agent_config_version",
            entity_id=str(version.id),
            details=None,
        )
    )
    await session.commit()

    get_state().set(config, version.id)
    return ApplyResult(status=ConfigVersionStatus.ACTIVE, version_id=version.id, errors=[])


async def _record_rejected(
    session: AsyncSession, *, source: str, raw_text: str, errors: list[str]
) -> ApplyResult:
    version = AgentConfigVersion(
        status=ConfigVersionStatus.REJECTED,
        source=source,
        raw_content=raw_text,
        parsed_config=None,
        validation_errors=errors,
    )
    session.add(version)
    await session.flush()

    session.add(
        AuditLog(
            actor=source,
            action="agent_gateway.config_rejected",
            entity_type="agent_config_version",
            entity_id=str(version.id),
            details={"errors": errors},
        )
    )
    await session.commit()

    # Deliberately no get_state() write here: last-known-good stays active.
    return ApplyResult(status=ConfigVersionStatus.REJECTED, version_id=version.id, errors=errors)


async def apply_config_text(session: AsyncSession, raw_text: str, *, source: str) -> ApplyResult:
    try:
        data = parse_config_text(raw_text)
        config = validate_config_dict(data, raw_text=raw_text)
    except ConfigLoadError as exc:
        return await _record_rejected(session, source=source, raw_text=raw_text, errors=exc.errors)
    return await _record_active(session, source=source, raw_text=raw_text, config=config)


async def apply_config_from_file(session: AsyncSession, path: Path, *, source: str) -> ApplyResult:
    try:
        raw_text = read_config_text(path)
    except ConfigLoadError as exc:
        return await _record_rejected(session, source=source, raw_text="", errors=exc.errors)
    return await apply_config_text(session, raw_text, source=source)
