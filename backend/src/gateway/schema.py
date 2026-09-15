"""Agent Gateway config file schema (Build Spec §6.1), as Pydantic models.

Every model rejects unknown keys (extra="forbid") and uses camelCase
aliases to match the JSON file verbatim while keeping Python attributes
snake_case. Validation here is the ONLY gate a config change has to pass
before being applied — see src/gateway/apply.py.

infra.riskThresholdRefs (RiskThresholdRefs below) is READ-ONLY: it exists
so the config file can *point at* the dual-control-gated risk limit
values, never to *set* them. This module intentionally defines no
function anywhere that writes a risk threshold — don't add one here or
in apply.py/service.py/cli.py. The real values live in Postgres behind
the stage/confirm dual-control flow built in Phase 6.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from src.gateway.roster import ROSTER_IDS


class _StrictCamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class LlmProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"


class BrokerId(StrEnum):
    ZERODHA = "zerodha"
    UPSTOX = "upstox"


class LlmProvidersConfig(_StrictCamelModel):
    order: list[LlmProvider] = Field(min_length=1)
    fallback_policy: Literal["next-on-failure"] = "next-on-failure"


class BrokerFailoverConfig(_StrictCamelModel):
    primary: BrokerId
    fallback: BrokerId

    @model_validator(mode="after")
    def _primary_and_fallback_must_differ(self) -> "BrokerFailoverConfig":
        if self.primary == self.fallback:
            raise ValueError("brokerFailover.primary and .fallback must be different brokers")
        return self


class RiskThresholdRefs(_StrictCamelModel):
    """Read-only pointers. See module docstring — never a write path."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    max_drawdown_pct: float = Field(gt=0)
    ws_latency_ms: float = Field(gt=0)


class InfraConfig(_StrictCamelModel):
    llm_providers: LlmProvidersConfig
    broker_failover: BrokerFailoverConfig
    risk_threshold_refs: RiskThresholdRefs


class AgentIdentityConfig(_StrictCamelModel):
    name: str = Field(min_length=1)
    emoji: str | None = None
    avatar: str | None = None
    theme: str | None = None
    voice: str | None = None


class AgentDefaultsConfig(_StrictCamelModel):
    model: str = "auto"
    heartbeat_enabled: bool = False
    skills: list[str] = Field(default_factory=lambda: ["market-data-read", "notification-send"])


class AgentEntryConfig(_StrictCamelModel):
    """One agent's overrides on top of AgentDefaultsConfig. Every field is
    optional — only what's actually being overridden needs to appear.
    """

    identity: AgentIdentityConfig | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_interval_minutes: int | None = Field(default=None, gt=0)
    model: str | None = None
    skills: list[str] | None = None


class AgentsConfig(_StrictCamelModel):
    defaults: AgentDefaultsConfig = Field(default_factory=AgentDefaultsConfig)
    entries: dict[str, AgentEntryConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _entries_must_reference_fixed_roster(self) -> "AgentsConfig":
        unknown = sorted(set(self.entries) - ROSTER_IDS)
        if unknown:
            raise ValueError(
                f"agents.entries references agent id(s) not in the fixed 24-agent "
                f"roster: {unknown}"
            )
        return self


class BindingMatch(_StrictCamelModel):
    channel: str = Field(min_length=1)
    account_id: str | None = None


class BindingConfig(_StrictCamelModel):
    agent_id: str
    match: BindingMatch

    @model_validator(mode="after")
    def _agent_id_must_be_in_roster(self) -> "BindingConfig":
        if self.agent_id not in ROSTER_IDS:
            raise ValueError(f"bindings[].agentId {self.agent_id!r} is not in the fixed roster")
        return self


class AgentToAgentAllowRule(_StrictCamelModel):
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    scope: str = Field(min_length=1)

    @model_validator(mode="after")
    def _agents_must_be_in_roster(self) -> "AgentToAgentAllowRule":
        for label, agent_id in (("from", self.from_agent), ("to", self.to_agent)):
            if agent_id not in ROSTER_IDS:
                raise ValueError(
                    f"agentToAgentPolicy.allow[].{label} {agent_id!r} is not in the "
                    f"fixed roster"
                )
        return self


class AgentToAgentPolicyConfig(_StrictCamelModel):
    # Default-deny is a non-negotiable rule (Build Spec §6.4) — constrained
    # to the single valid literal so a config file can never flip it.
    default: Literal["deny"] = "deny"
    allow: list[AgentToAgentAllowRule] = Field(default_factory=list)


class TradingOSConfig(_StrictCamelModel):
    version: Literal[1] = 1
    infra: InfraConfig
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    bindings: list[BindingConfig] = Field(default_factory=list)
    agent_to_agent_policy: AgentToAgentPolicyConfig = Field(
        default_factory=AgentToAgentPolicyConfig
    )
