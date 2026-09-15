import pytest
from pydantic import ValidationError

from src.agents.roster import (
    AGENT_CAPABILITIES,
    PIPELINE_NODE_AGENTS,
    agent_has_capability,
    capabilities_for,
)
from src.gateway.roster import ROSTER_IDS
from src.gateway.schema import TradingOSConfig

_BASE_CONFIG = {
    "version": 1,
    "infra": {
        "llmProviders": {"order": ["anthropic"]},
        "brokerFailover": {"primary": "zerodha", "fallback": "upstox"},
        "riskThresholdRefs": {"maxDrawdownPct": 15, "wsLatencyMs": 100},
    },
}


def test_every_roster_agent_has_at_least_one_capability():
    assert set(AGENT_CAPABILITIES) == ROSTER_IDS
    for agent_id in ROSTER_IDS:
        assert len(capabilities_for(agent_id)) > 0


def test_agent_has_capability_lookup():
    assert agent_has_capability("ceo-agent", "graph.ceo_kickoff")
    assert not agent_has_capability("ceo-agent", "graph.deployment")
    assert not agent_has_capability("not-a-real-agent", "anything")


def test_pipeline_has_13_nodes_each_owned_by_a_distinct_agent():
    assert len(PIPELINE_NODE_AGENTS) == 13
    agent_ids = [agent_id for _, agent_id in PIPELINE_NODE_AGENTS]
    assert len(set(agent_ids)) == 13
    assert set(agent_ids).issubset(ROSTER_IDS)


def test_audit_agent_cannot_be_disabled_via_gateway_config():
    with pytest.raises(ValidationError):
        TradingOSConfig.model_validate(
            {**_BASE_CONFIG, "agents": {"entries": {"audit-agent": {"enabled": False}}}}
        )


def test_other_agents_can_be_disabled_via_gateway_config():
    config = TradingOSConfig.model_validate(
        {**_BASE_CONFIG, "agents": {"entries": {"market-analyst": {"enabled": False}}}}
    )
    assert config.agents.entries["market-analyst"].enabled is False


def test_audit_agent_enabled_true_is_still_allowed():
    config = TradingOSConfig.model_validate(
        {**_BASE_CONFIG, "agents": {"entries": {"audit-agent": {"enabled": True}}}}
    )
    assert config.agents.entries["audit-agent"].enabled is True
