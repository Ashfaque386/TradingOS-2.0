from pathlib import Path

import pytest

from src.gateway.apply import apply_config_text
from src.gateway.loader import (
    ConfigLoadError,
    compute_effective_agents,
    load_and_validate,
    parse_config_text,
)
from src.models.agent_config_version import ConfigVersionStatus

VALID_CONFIG_JSON5 = """
{
  // JSON5: comments and trailing commas are fine.
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic', 'openai'], },
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
}
"""


def test_valid_json5_with_comments_and_trailing_commas_loads(tmp_path: Path):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text(VALID_CONFIG_JSON5)

    config = load_and_validate(config_path)

    assert config.version == 1
    effective = compute_effective_agents(config)
    assert len(effective) == 24
    assert effective["ceo-agent"].identity_name == "CEO"
    assert effective["ceo-agent"].heartbeat_interval_minutes == 10
    # An agent with no override still gets the roster/defaults fallback.
    assert effective["risk-manager"].identity_name == "Risk Manager"
    assert effective["risk-manager"].heartbeat_enabled is False


def test_missing_file_raises_config_load_error_not_crash(tmp_path: Path):
    with pytest.raises(ConfigLoadError):
        load_and_validate(tmp_path / "does-not-exist.json")


def test_invalid_json5_syntax_raises_config_load_error_with_errors(tmp_path: Path):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text("{ this is not valid json5 !!! ")

    with pytest.raises(ConfigLoadError) as exc_info:
        load_and_validate(config_path)

    assert exc_info.value.errors
    assert exc_info.value.raw_text is not None


def test_schema_violation_raises_config_load_error_with_readable_errors(tmp_path: Path):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text(VALID_CONFIG_JSON5.replace("version: 1,", "version: 1,\n  bogus: true,"))

    with pytest.raises(ConfigLoadError) as exc_info:
        load_and_validate(config_path)

    assert any("bogus" in err for err in exc_info.value.errors)


def test_non_object_top_level_raises_config_load_error(tmp_path: Path):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text("[1, 2, 3]")

    with pytest.raises(ConfigLoadError):
        load_and_validate(config_path)


# A Python client's json.dumps writes the agent emoji as an escaped UTF-16
# surrogate pair. json5 decoded that into two lone surrogates, which then
# crashed storing the config version in Postgres (PUT /gateway/config 500,
# seen live in the Phase 17 local pass).
_ESCAPED_EMOJI_CONFIG = (
    '{"version": 1, "infra": {"llmProviders": {"order": ["anthropic"]}, '
    '"brokerFailover": {"primary": "zerodha", "fallback": "upstox"}, '
    '"riskThresholdRefs": {"maxDrawdownPct": 15, "wsLatencyMs": 100}}, '
    '"agents": {"entries": {"ceo-agent": {"identity": {"name": "CEO", '
    '"emoji": "\\ud83e\\udde0"}}}}}'
)


def test_escaped_surrogate_pair_decodes_to_one_character():
    data = parse_config_text(_ESCAPED_EMOJI_CONFIG)
    assert data["agents"]["entries"]["ceo-agent"]["identity"]["emoji"] == "🧠"


def test_unpaired_surrogate_escape_is_a_config_error_not_a_crash():
    with pytest.raises(ConfigLoadError, match="unpaired"):
        parse_config_text('{"version": 1, "x": "\\ud83e"}')


async def test_escaped_emoji_config_applies_and_is_stored(db_session_factory):
    async with db_session_factory() as db:
        result = await apply_config_text(db, _ESCAPED_EMOJI_CONFIG, source="t")
    assert result.status == ConfigVersionStatus.ACTIVE, result.errors
