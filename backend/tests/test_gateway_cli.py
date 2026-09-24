"""CLI tests run as plain sync functions (not async def): each command
internally does its own asyncio.run(), which cannot be called from inside
a pytest-asyncio event loop already running for the test itself. This
matches how the CLI is really invoked — a synchronous console script.
"""

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.core.config import get_settings
from src.core.db import engine
from src.gateway.cli import cli
from src.models import Base

VALID_CONFIG_JSON5 = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic', 'openai'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
  agents: {
    entries: {
      'ceo-agent': {
        identity: { name: 'CEO', emoji: '🧠', theme: 'cyan' },
        heartbeatEnabled: true,
        heartbeatIntervalMinutes: 10,
      },
    },
  },
  bindings: [ { agentId: 'ceo-agent', match: { channel: 'telegram', accountId: 'ops' } } ],
}
"""


@pytest.fixture
def clean_db():
    assert "test" in get_settings().database_url, "refusing to wipe a non-test database"

    async def _reset() -> None:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            # Same reasoning as cli.py's _run(): don't leave a pooled
            # connection bound to this asyncio.run()'s event loop for the
            # test body's own asyncio.run() calls to inherit.
            await engine.dispose()

    asyncio.run(_reset())


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "tradingos.config.json"
    path.write_text(VALID_CONFIG_JSON5)
    return path


def _invoke(config_path: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["--config", str(config_path), *args])


def test_config_validate_ok(clean_db, config_path: Path):
    result = _invoke(config_path, "config", "validate")
    assert result.exit_code == 0
    assert "OK" in result.output


def test_config_validate_invalid_config_exits_nonzero(clean_db, tmp_path: Path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{ not valid json5 !!! ")
    result = _invoke(bad_path, "config", "validate")
    assert result.exit_code != 0
    assert "INVALID" in result.output


def test_agents_list_table(clean_db, config_path: Path):
    result = _invoke(config_path, "agents", "list")
    assert result.exit_code == 0
    assert "ceo-agent" in result.output
    assert result.output.count("\n") >= 24  # one row per fixed-roster agent


def test_agents_list_json(clean_db, config_path: Path):
    result = _invoke(config_path, "agents", "list", "--json")
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 30
    ceo = next(r for r in rows if r["agentId"] == "ceo-agent")
    assert ceo["name"] == "CEO"
    assert ceo["heartbeatIntervalMinutes"] == 10


def test_agents_set_identity_applies_and_persists(clean_db, config_path: Path):
    result = _invoke(
        config_path,
        "agents",
        "set-identity",
        "--agent",
        "evaluator",
        "--name",
        "Eval Bot",
        "--emoji",
        "🧪",
    )
    assert result.exit_code == 0
    assert "Applied as config version 1" in result.output

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert evaluator["name"] == "Eval Bot"
    assert evaluator["emoji"] == "🧪"


def test_agents_set_identity_unknown_agent_rejected_by_cli_parsing(clean_db, config_path: Path):
    result = _invoke(
        config_path, "agents", "set-identity", "--agent", "not-a-real-agent", "--name", "X"
    )
    assert result.exit_code != 0


def test_agents_bind_and_unbind(clean_db, config_path: Path):
    bind_result = _invoke(
        config_path,
        "agents",
        "bind",
        "--agent",
        "evaluator",
        "--channel",
        "discord",
        "--account",
        "room",
    )
    assert bind_result.exit_code == 0

    unbind_result = _invoke(
        config_path,
        "agents",
        "unbind",
        "--agent",
        "evaluator",
        "--channel",
        "discord",
        "--account",
        "room",
    )
    assert unbind_result.exit_code == 0


def test_agents_skills_grant_and_revoke(clean_db, config_path: Path):
    grant = _invoke(
        config_path, "agents", "skills", "grant", "--agent", "evaluator", "--skill", "x-skill"
    )
    assert grant.exit_code == 0

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert "x-skill" in evaluator["skills"]

    revoke = _invoke(
        config_path, "agents", "skills", "revoke", "--agent", "evaluator", "--skill", "x-skill"
    )
    assert revoke.exit_code == 0

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert "x-skill" not in evaluator["skills"]


def test_agents_heartbeat_enable_and_disable(clean_db, config_path: Path):
    enable = _invoke(
        config_path, "agents", "heartbeat", "enable", "--agent", "evaluator", "--interval", "7"
    )
    assert enable.exit_code == 0

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert evaluator["heartbeatEnabled"] is True
    assert evaluator["heartbeatIntervalMinutes"] == 7

    disable = _invoke(config_path, "agents", "heartbeat", "disable", "--agent", "evaluator")
    assert disable.exit_code == 0

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert evaluator["heartbeatEnabled"] is False


def test_config_rollback(clean_db, config_path: Path):
    original = _invoke(
        config_path, "agents", "set-identity", "--agent", "evaluator", "--name", "Original"
    )
    assert original.exit_code == 0
    assert "Applied as config version 1" in original.output

    changed = _invoke(
        config_path, "agents", "set-identity", "--agent", "evaluator", "--name", "Changed"
    )
    assert changed.exit_code == 0
    assert "Applied as config version 2" in changed.output

    rollback = _invoke(config_path, "config", "rollback", "--to-version", "1")
    assert rollback.exit_code == 0
    assert "Applied as config version 3" in rollback.output

    rows = json.loads(_invoke(config_path, "agents", "list", "--json").output)
    evaluator = next(r for r in rows if r["agentId"] == "evaluator")
    assert evaluator["name"] == "Original"  # reverted, not stuck on "Changed"


def test_config_rollback_unknown_version_fails(clean_db, config_path: Path):
    result = _invoke(config_path, "config", "rollback", "--to-version", "99999")
    assert result.exit_code != 0


def test_doctor_no_issues(clean_db, config_path: Path):
    result = _invoke(config_path, "doctor")
    assert result.exit_code == 0
    assert "No issues found" in result.output


def test_doctor_finds_and_fixes_duplicate_bindings(clean_db, config_path: Path):
    _invoke(config_path, "agents", "bind", "--agent", "evaluator", "--channel", "discord")
    _invoke(config_path, "agents", "bind", "--agent", "evaluator", "--channel", "discord")

    report_only = _invoke(config_path, "doctor")
    assert report_only.exit_code == 0
    assert "duplicate binding" in report_only.output
    assert "found" in report_only.output

    fixed = _invoke(config_path, "doctor", "--fix")
    assert fixed.exit_code == 0
    assert "fixed" in fixed.output

    clean = _invoke(config_path, "doctor")
    assert "No issues found" in clean.output
