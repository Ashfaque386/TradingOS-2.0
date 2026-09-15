"""Strict, versioned state for the 13-node LangGraph pipeline (Build Spec
§7.2, src/agents/graph.py). extra="forbid" makes every node's read/write
surface exactly the fields declared here -- an ad hoc extra key a node
tries to set is a validation error, not a silent addition. state_version
exists so a later phase changing this shape has an explicit migration
point rather than an implicit one.

Every node function (src/agents/graph.py) returns a partial dict of the
fields it changed, not a whole new TradingOSGraphState -- that's the
LangGraph idiom (the compiled graph applies each node's returned dict as an
update over the running state) and lets `node_log` accumulate correctly
across the run.
"""

from pydantic import BaseModel, ConfigDict, Field


class TradingOSGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_version: int = 1
    objective: str
    run_id: str | None = None

    ceo_brief: dict | None = None
    market_analysis: dict | None = None
    strategy: dict | None = None
    options_legs: dict | None = None
    generated_code: str | None = None
    compliance_verdict: dict | None = None
    validation_result: dict | None = None
    validation_attempts: int = 0
    backtest_metrics: dict | None = None
    evaluation_verdict: dict | None = None
    optimization_result: dict | None = None
    risk_assessment: dict | None = None
    deployment_result: dict | None = None

    rejection_count: int = 0
    node_log: list[str] = Field(default_factory=list)
