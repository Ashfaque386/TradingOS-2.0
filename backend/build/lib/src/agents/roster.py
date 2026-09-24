"""Executable capabilities for the fixed 24-agent roster (Build Spec §7.1,
§7.2). This is a *separate* concept from src/gateway/schema.py's per-agent
`skills` grants (the Skill Registry, §16, enforced in src/agents/tools):

- Capabilities here are which LangGraph pipeline nodes (src/agents/graph.py)
  and other structural, domain-level operations an agent is wired to. Fixed
  at code level — a plain dict, never inserted/edited through the Gateway or
  any DB row, exactly like src/gateway/roster.ROSTER itself.
- Skills (src/agents/tools/registry.py) are the separately-grantable,
  config-driven Skill Registry entries (market-data-read, notification-send,
  etc.) an agent may invoke — those grants ARE Gateway-config-editable
  (AgentDefaultsConfig.skills / AgentEntryConfig.skills, already built in
  Phase 1) because granting/revoking a skill is an operational decision, not
  a change to what role the agent plays in the pipeline.

Every agent id used as a key below must be one of the fixed roster's 24 ids
(asserted at import time) — this module cannot silently drift from
src/gateway/roster.ROSTER.
"""

from src.gateway.roster import ROSTER_IDS

# One entry per pipeline node (src/agents/graph.py's 13 nodes) plus a few
# operational, non-pipeline capabilities for the remaining roster agents.
AGENT_CAPABILITIES: dict[str, frozenset[str]] = {
    "ceo-agent": frozenset({"graph.ceo_kickoff", "graph.ceo_final_review"}),
    "ceo-chat-interface": frozenset({"chat-interface"}),
    "market-analyst": frozenset({"graph.market_analysis", "market-data-read"}),
    "news-agent": frozenset({"research_scaffold_news"}),
    "sentiment-agent": frozenset({"research_scaffold_sentiment"}),
    "strategy-generator": frozenset({"graph.strategy_generation"}),
    "options-strategy-agent": frozenset({"graph.options_strategy", "option-chain-read"}),
    "python-code-generator": frozenset({"graph.code_generation"}),
    "python-validator": frozenset({"graph.code_validation", "code-format-lint"}),
    "backtesting-agent": frozenset({"graph.backtesting", "sandbox-dry-run"}),
    "optimization-agent": frozenset({"graph.optimization"}),
    "evaluator": frozenset({"graph.evaluation"}),
    "risk-manager": frozenset({"graph.risk_assessment", "portfolio-status-read"}),
    "compliance-agent": frozenset({"graph.compliance_check"}),
    "audit-agent": frozenset({"audit-log-read"}),
    "deployment-agent": frozenset({"graph.deployment"}),
    "portfolio-manager-agent": frozenset({"portfolio-status-read"}),
    "memory-agent": frozenset({"graph.memory_ingest"}),
    "data-ingestion-agent": frozenset({"market-data-read", "option-chain-read"}),
    "scheduler-agent": frozenset({"schedule-manage"}),
    "notification-agent": frozenset({"notification-send"}),
    "skill-registry-manager": frozenset({"skill-registry-manage"}),
    "execution-agent": frozenset({"order-execute"}),
    "paper-trading-engine": frozenset({"paper-order-execute"}),
    # Phase 19 additions -- docs/phase19-audit.md. None are pipeline nodes;
    # each runs on its own schedule (src/orchestration/screener_scheduler.py,
    # post_trade_review_scheduler.py, investor_reporting_scheduler.py) the
    # same way data-ingestion-agent and notification-agent already do.
    "screener-agent": frozenset({"instrument-screen"}),
    # fundamentals-agent/valuation-agent/macro-agent: no real data source
    # exists anywhere in this codebase for any of these (confirmed by the
    # Phase 19 audit's exhaustive grep, docs/phase19-audit.md §3.2) -- the
    # capability names below are honest about what they'd read, and the
    # orchestration layer that would serve them returns an explicit gap
    # notice rather than a fabricated number. See src/orchestration/
    # fundamentals.py.
    "fundamentals-agent": frozenset({"fundamentals-read"}),
    "valuation-agent": frozenset({"valuation-read"}),
    "macro-agent": frozenset({"macro-calendar-read"}),
    "post-trade-review-agent": frozenset({"trade-review-write"}),
    "investor-reporting-agent": frozenset({"investor-report-generate"}),
}

assert (
    set(AGENT_CAPABILITIES) == ROSTER_IDS
), "AGENT_CAPABILITIES must have exactly one entry per fixed-roster agent id"


def capabilities_for(agent_id: str) -> frozenset[str]:
    return AGENT_CAPABILITIES.get(agent_id, frozenset())


def agent_has_capability(agent_id: str, capability: str) -> bool:
    return capability in capabilities_for(agent_id)


# The 13 LangGraph pipeline nodes (Build Spec §7.2), in the order they were
# specified, mapped to the roster agent that owns each node's logic. graph.py
# builds its node list from this so the two never drift apart. Node names
# are suffixed "_step" where they'd otherwise collide with a
# TradingOSGraphState field name of the same underlying concept (LangGraph
# forbids a node name equal to a state schema key).
PIPELINE_NODE_AGENTS: tuple[tuple[str, str], ...] = (
    ("ceo_kickoff", "ceo-agent"),
    ("market_analysis_step", "market-analyst"),
    ("strategy_generation", "strategy-generator"),
    ("options_strategy", "options-strategy-agent"),
    ("code_generation", "python-code-generator"),
    ("compliance_check", "compliance-agent"),
    ("code_validation", "python-validator"),
    ("backtesting", "backtesting-agent"),
    ("evaluation", "evaluator"),
    ("optimization", "optimization-agent"),
    ("risk_assessment_step", "risk-manager"),
    ("deployment", "deployment-agent"),
    ("memory_ingest", "memory-agent"),
)

assert len(PIPELINE_NODE_AGENTS) == 13, "Build Spec §7.2 pipeline has exactly 13 nodes"

# Phase 19 (docs/phase19-audit.md §3.2): these agents' read capability
# names a real thing (fundamentals, valuation, macro/economic calendar)
# that no data source anywhere in this codebase actually provides -- the
# audit's exhaustive grep confirmed zero fundamentals/macro fields exist
# in any model. Frontend surfaces (Agent Fleet's detail view) use this set
# to render an honest gap notice for these three agents specifically,
# rather than a fabricated number or a silently-empty panel that looks
# like real data just hasn't loaded yet.
NO_DATA_SOURCE_AGENTS: frozenset[str] = frozenset(
    {"fundamentals-agent", "valuation-agent", "macro-agent"}
)

assert (
    NO_DATA_SOURCE_AGENTS <= ROSTER_IDS
), "NO_DATA_SOURCE_AGENTS must only name real roster agents"
