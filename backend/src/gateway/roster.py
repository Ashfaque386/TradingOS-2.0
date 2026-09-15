"""The fixed 24-agent roster (Build Spec §7.1). Curated, not user-creatable:
the Agent Gateway configures these 24 agents, it does not create or delete
them. Every agentId referenced anywhere in the config file (agents.entries,
bindings, agentToAgentPolicy) must be one of these.
"""

from enum import StrEnum
from typing import NamedTuple


class Department(StrEnum):
    EXECUTIVE = "Executive"
    MARKET_INTELLIGENCE = "Market Intelligence"
    RESEARCH = "Research"
    QUANT = "Quant"
    RISK_AND_GOVERNANCE = "Risk & Governance"
    PORTFOLIO = "Portfolio"
    OPERATIONS = "Operations"


class RosterAgent(NamedTuple):
    agent_id: str
    display_name: str
    department: Department
    # Audit Agent cannot be disabled (Build Spec §7.1); tracked here so
    # later phases enforcing an enable/disable toggle can't overlook it.
    can_disable: bool = True


ROSTER: tuple[RosterAgent, ...] = (
    RosterAgent("ceo-agent", "CEO Agent", Department.EXECUTIVE),
    RosterAgent("ceo-chat-interface", "CEO Chat Interface", Department.EXECUTIVE),
    RosterAgent("market-analyst", "Market Analyst", Department.MARKET_INTELLIGENCE),
    RosterAgent("news-agent", "News Agent", Department.MARKET_INTELLIGENCE),
    RosterAgent("sentiment-agent", "Sentiment Agent", Department.MARKET_INTELLIGENCE),
    RosterAgent("strategy-generator", "Strategy Generator", Department.RESEARCH),
    RosterAgent("options-strategy-agent", "Options Strategy Agent", Department.RESEARCH),
    RosterAgent("python-code-generator", "Python Code Generator", Department.QUANT),
    RosterAgent("python-validator", "Python Validator", Department.QUANT),
    RosterAgent("backtesting-agent", "Backtesting", Department.QUANT),
    RosterAgent("optimization-agent", "Optimization", Department.QUANT),
    RosterAgent("evaluator", "Evaluator", Department.QUANT),
    RosterAgent("risk-manager", "Risk Manager", Department.RISK_AND_GOVERNANCE),
    RosterAgent("compliance-agent", "Compliance", Department.RISK_AND_GOVERNANCE),
    RosterAgent("audit-agent", "Audit Agent", Department.RISK_AND_GOVERNANCE, can_disable=False),
    RosterAgent("deployment-agent", "Deployment", Department.RISK_AND_GOVERNANCE),
    RosterAgent("portfolio-manager-agent", "Portfolio Manager Agent", Department.PORTFOLIO),
    RosterAgent("memory-agent", "Memory Agent", Department.OPERATIONS),
    RosterAgent("data-ingestion-agent", "Data Ingestion Agent", Department.OPERATIONS),
    RosterAgent("scheduler-agent", "Scheduler Agent", Department.OPERATIONS),
    RosterAgent("notification-agent", "Notification Agent", Department.OPERATIONS),
    RosterAgent("skill-registry-manager", "Skill Registry Manager", Department.OPERATIONS),
    RosterAgent("execution-agent", "Execution Agent", Department.OPERATIONS),
    RosterAgent("paper-trading-engine", "Paper Trading Engine", Department.OPERATIONS),
)

assert len(ROSTER) == 24, f"Fixed roster must have exactly 24 agents, got {len(ROSTER)}"

ROSTER_BY_ID: dict[str, RosterAgent] = {agent.agent_id: agent for agent in ROSTER}

ROSTER_IDS: frozenset[str] = frozenset(ROSTER_BY_ID)


def is_valid_agent_id(agent_id: str) -> bool:
    return agent_id in ROSTER_BY_ID
