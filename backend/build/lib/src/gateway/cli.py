"""`tradingos-cli` (Build Spec §6.3) — a thin wrapper over src/gateway/service.py.
Every command here calls the exact same function a future API endpoint
would call; this file's only job is argument parsing and human-readable
output.
"""

import asyncio
import json
from pathlib import Path

import click

from src.core.config import get_settings
from src.core.db import AsyncSessionLocal, engine
from src.gateway import service
from src.gateway.loader import ConfigLoadError, EffectiveAgentConfig
from src.gateway.roster import ROSTER_IDS
from src.models.agent_config_version import ConfigVersionStatus


def _default_config_path() -> Path:
    return Path(get_settings().agent_gateway_config_path)


def _run(coro):
    # tradingos-cli is a short-lived, single-invocation process: dispose
    # the DB engine's connection pool before this event loop closes so a
    # process-internal caller invoking the CLI's Click group more than
    # once (as the test suite does) never reuses a pooled connection
    # across a closed event loop — asyncpg connections are loop-bound.
    async def _wrapped():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_wrapped())


def _fail(message: str) -> None:
    raise click.ClickException(message)


def _report_apply_result(result: service.ApplyResult) -> None:
    if result.status == ConfigVersionStatus.ACTIVE:
        click.echo(f"Applied as config version {result.version_id}.")
    else:
        _fail(
            f"Change was rejected (version {result.version_id}):\n"
            + "\n".join(f"  - {err}" for err in result.errors)
        )


def _agent_to_row(agent: EffectiveAgentConfig) -> dict:
    return {
        "agentId": agent.agent_id,
        "department": agent.department,
        "name": agent.identity_name,
        "emoji": agent.emoji,
        "model": agent.model,
        "heartbeatEnabled": agent.heartbeat_enabled,
        "heartbeatIntervalMinutes": agent.heartbeat_interval_minutes,
        "skills": list(agent.skills),
    }


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to tradingos.config.json (defaults to AGENT_GATEWAY_CONFIG_PATH).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path or _default_config_path()


@cli.group()
def agents() -> None:
    """Manage the fixed 24-agent roster's config."""


@agents.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of a table.")
@click.pass_context
def agents_list(ctx: click.Context, as_json: bool) -> None:
    try:
        rows = service.list_agents(ctx.obj["config_path"])
    except ConfigLoadError as exc:
        _fail(str(exc))
        return

    if as_json:
        click.echo(json.dumps([_agent_to_row(a) for a in rows], indent=2, ensure_ascii=False))
        return

    for agent in rows:
        heartbeat = (
            f"every {agent.heartbeat_interval_minutes}m"
            if agent.heartbeat_enabled and agent.heartbeat_interval_minutes
            else ("on" if agent.heartbeat_enabled else "off")
        )
        click.echo(
            f"{agent.agent_id:28} {agent.department:22} "
            f"{(agent.emoji or ' ')} {agent.identity_name:24} "
            f"model={agent.model:20} heartbeat={heartbeat}"
        )


@agents.command("set-identity")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--name", default=None)
@click.option("--emoji", default=None)
@click.option("--avatar", default=None)
@click.option("--theme", default=None)
@click.option("--voice", default=None)
@click.pass_context
def agents_set_identity(
    ctx: click.Context,
    agent_id: str,
    name: str | None,
    emoji: str | None,
    avatar: str | None,
    theme: str | None,
    voice: str | None,
) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.set_identity(
                session,
                ctx.obj["config_path"],
                agent_id,
                name=name,
                emoji=emoji,
                avatar=avatar,
                theme=theme,
                voice=voice,
            )

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@agents.command("bind")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--channel", required=True)
@click.option("--account", "account_id", default=None)
@click.pass_context
def agents_bind(ctx: click.Context, agent_id: str, channel: str, account_id: str | None) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.bind_agent(
                session, ctx.obj["config_path"], agent_id, channel, account_id
            )

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@agents.command("unbind")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--channel", required=True)
@click.option("--account", "account_id", default=None)
@click.pass_context
def agents_unbind(ctx: click.Context, agent_id: str, channel: str, account_id: str | None) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.unbind_agent(
                session, ctx.obj["config_path"], agent_id, channel, account_id
            )

    try:
        _report_apply_result(_run(go()))
    except ConfigLoadError as exc:
        _fail(str(exc))


@agents.group()
def skills() -> None:
    """Grant or revoke a skill for an agent."""


@skills.command("grant")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--skill", required=True)
@click.pass_context
def skills_grant(ctx: click.Context, agent_id: str, skill: str) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.grant_skill(session, ctx.obj["config_path"], agent_id, skill)

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@skills.command("revoke")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--skill", required=True)
@click.pass_context
def skills_revoke(ctx: click.Context, agent_id: str, skill: str) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.revoke_skill(session, ctx.obj["config_path"], agent_id, skill)

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@agents.group()
def heartbeat() -> None:
    """Toggle or tune an agent's heartbeat."""


@heartbeat.command("enable")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.option("--interval", "interval_minutes", required=True, type=click.IntRange(min=1))
@click.pass_context
def heartbeat_enable(ctx: click.Context, agent_id: str, interval_minutes: int) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.set_heartbeat(
                session,
                ctx.obj["config_path"],
                agent_id,
                enabled=True,
                interval_minutes=interval_minutes,
            )

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@heartbeat.command("disable")
@click.option("--agent", "agent_id", required=True, type=click.Choice(sorted(ROSTER_IDS)))
@click.pass_context
def heartbeat_disable(ctx: click.Context, agent_id: str) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.set_heartbeat(
                session, ctx.obj["config_path"], agent_id, enabled=False
            )

    try:
        _report_apply_result(_run(go()))
    except (ConfigLoadError, service.ServiceError) as exc:
        _fail(str(exc))


@cli.group()
def config() -> None:
    """Validate or roll back the config file."""


@config.command("validate")
@click.option(
    "--file",
    "file_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override config path.",
)
@click.pass_context
def config_validate(ctx: click.Context, file_path: Path | None) -> None:
    path = file_path or ctx.obj["config_path"]
    result = service.validate_config(path)
    if result.ok:
        click.echo(f"{path}: OK")
        return
    _fail(f"{path}: INVALID\n" + "\n".join(f"  - {err}" for err in result.errors))


@config.command("rollback")
@click.option("--to-version", "to_version", required=True, type=int)
@click.pass_context
def config_rollback(ctx: click.Context, to_version: int) -> None:
    async def go() -> service.ApplyResult:
        async with AsyncSessionLocal() as session:
            return await service.rollback_config(session, ctx.obj["config_path"], to_version)

    try:
        _report_apply_result(_run(go()))
    except service.ServiceError as exc:
        _fail(str(exc))


@cli.command("doctor")
@click.option(
    "--fix", is_flag=True, help="Repair known-safe issues instead of just reporting them."
)
@click.pass_context
def doctor(ctx: click.Context, fix: bool) -> None:
    async def go() -> service.DoctorReport:
        async with AsyncSessionLocal() as session:
            return await service.doctor(session, ctx.obj["config_path"], fix=fix)

    report = _run(go())

    if not report.ok:
        _fail(
            "Config does not parse/validate:\n" + "\n".join(f"  - {e}" for e in report.load_errors)
        )

    if not report.issues:
        click.echo("No issues found.")
        return

    for issue in report.issues:
        status = "fixed" if issue.fixed else "found (rerun with --fix to repair)"
        click.echo(f"- {issue.description} [{status}]")

    if report.apply_result is not None:
        _report_apply_result(report.apply_result)


if __name__ == "__main__":
    cli()
