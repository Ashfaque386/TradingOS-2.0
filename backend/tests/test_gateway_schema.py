import copy

import pytest
from pydantic import ValidationError

from src.gateway.schema import TradingOSConfig

VALID_CONFIG_DICT = {
    "version": 1,
    "infra": {
        "llmProviders": {"order": ["anthropic", "openai"]},
        "brokerFailover": {"primary": "zerodha", "fallback": "upstox"},
        "riskThresholdRefs": {"maxDrawdownPct": 15, "wsLatencyMs": 100},
    },
    "agents": {
        "entries": {
            "ceo-agent": {
                "identity": {"name": "CEO", "emoji": "🧠", "theme": "cyan"},
                "heartbeatEnabled": True,
                "heartbeatIntervalMinutes": 10,
            }
        }
    },
    "bindings": [{"agentId": "ceo-agent", "match": {"channel": "telegram", "accountId": "ops"}}],
    "agentToAgentPolicy": {
        "allow": [{"from": "risk-manager", "to": "ceo-agent", "scope": "read-artefacts"}]
    },
}


def test_valid_config_accepted():
    config = TradingOSConfig.model_validate(VALID_CONFIG_DICT)
    assert config.version == 1
    assert config.agents.entries["ceo-agent"].identity.name == "CEO"
    assert config.agent_to_agent_policy.allow[0].from_agent == "risk-manager"
    assert config.agent_to_agent_policy.default == "deny"


def test_unknown_top_level_key_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["notARealField"] = True
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_unknown_nested_key_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["infra"]["riskThresholdRefs"]["extraField"] = 1
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_wrong_type_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["infra"]["riskThresholdRefs"]["maxDrawdownPct"] = "not-a-number"
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_unknown_llm_provider_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["infra"]["llmProviders"]["order"] = ["not-a-real-provider"]
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_non_roster_agent_id_in_entries_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["agents"]["entries"]["not-a-real-agent"] = {"model": "auto"}
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_non_roster_agent_id_in_bindings_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["bindings"].append({"agentId": "not-a-real-agent", "match": {"channel": "slack"}})
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_non_roster_agent_id_in_allow_rule_rejected():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["agentToAgentPolicy"]["allow"].append(
        {"from": "not-a-real-agent", "to": "ceo-agent", "scope": "read-artefacts"}
    )
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_agent_to_agent_policy_default_cannot_be_flipped_to_allow():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["agentToAgentPolicy"]["default"] = "allow"
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_broker_failover_primary_and_fallback_must_differ():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["infra"]["brokerFailover"] = {"primary": "zerodha", "fallback": "zerodha"}
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_heartbeat_interval_must_be_positive():
    bad = copy.deepcopy(VALID_CONFIG_DICT)
    bad["agents"]["entries"]["ceo-agent"]["heartbeatIntervalMinutes"] = 0
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(bad)


def test_risk_threshold_refs_is_frozen():
    """Read-only pointers (Build Spec §6.2) — belt-and-suspenders at the
    type level on top of there being no write-path function anywhere.
    """
    config = TradingOSConfig.model_validate(VALID_CONFIG_DICT)
    with pytest.raises(ValidationError):
        config.infra.risk_threshold_refs.max_drawdown_pct = 999
