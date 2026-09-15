from pathlib import Path

import pytest

from src.gateway.loader import ConfigLoadError, compute_effective_agents, load_and_validate

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
