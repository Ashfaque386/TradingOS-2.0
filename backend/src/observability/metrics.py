"""Prometheus metrics (Build Spec §19): "WS latency, agent node duration,
order dispatch latency, LLM token usage, trading-holiday gauge" -- exactly
those five, each wired at the one real choke point every corresponding
Phase 6/3/8/3/10 subsystem already funnels through, so no call site
outside this module needs to know a metric exists.

`GET /metrics` (src/api/routes/health.py) exposes these in the standard
Prometheus text exposition format, unauthenticated -- the conventional
posture for a scrape endpoint (network-level access control, not
app-level RBAC, is how a real deployment would restrict it; Build Spec
§20's RBAC table has no role for "metrics scraper" and inventing one would
be over-engineering for a self-hosted, solo-operator deployment).
"""

import asyncio
from datetime import UTC, datetime

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from src.data.nse_calendar import is_nse_trading_day

logger = structlog.get_logger(__name__)

# WebSocket / tick-relay latency (Build Spec §8's WebSocketLatencyGuard,
# 100ms default threshold -- see src.engine.risk.latency_guard).
ws_latency_seconds = Histogram(
    "tradingos_ws_latency_seconds",
    "Observed WebSocket/tick-relay latency",
    buckets=(0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.5, 1.0),
)

# LangGraph node execution duration (Build Spec §7.2's 13-node pipeline),
# labeled by node name -- src.agents.graph.build_graph's per-node wrapper
# is the single place every node call passes through.
agent_node_duration_seconds = Histogram(
    "tradingos_agent_node_duration_seconds",
    "LangGraph node execution duration",
    labelnames=("node",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Order dispatch latency (tick -> broker handoff), measured against
# settings.order_dispatch_latency_budget_ms -- src.brokers.resilient.
# ResilientBrokerAdapter.place_order is the one function every order
# submission (Phase 8 Shadow Mode, Phase 9 live trading) goes through.
order_dispatch_latency_seconds = Histogram(
    "tradingos_order_dispatch_latency_seconds",
    "Broker order dispatch latency (tick to broker handoff)",
    labelnames=("broker",),
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0),
)
order_dispatch_budget_breached_total = Counter(
    "tradingos_order_dispatch_budget_breached_total",
    "Order dispatches that exceeded the documented latency budget",
    labelnames=("broker",),
)

# LLM token usage (Build Spec §7.1's multi-provider router), labeled by
# provider and prompt/completion -- src.agents.llm_router.LlmRouter.complete
# is the single choke point every agent's LLM call passes through.
llm_token_usage_total = Counter(
    "tradingos_llm_token_usage_total",
    "LLM tokens consumed, by provider and token type",
    labelnames=("provider", "token_type"),
)

# 1 when today is NOT an NSE trading day (weekend or holiday), 0 when it
# is -- named to match Build Spec §19's literal "trading-holiday gauge",
# not a generic "market open" gauge (see src.data.nse_calendar for what
# "trading day" means here, honest calendar gaps included).
trading_holiday_gauge = Gauge(
    "tradingos_trading_holiday",
    "1 if today is not an NSE trading day (weekend or known holiday), else 0",
)

TRADING_HOLIDAY_GAUGE_UPDATE_INTERVAL_SECONDS = 300

# Captured once at import time -- the honest anchor for the vitals summary
# below. A Prometheus Counter/Histogram has no calendar-day boundary built
# in; every figure `build_vitals_summary()` reports is cumulative since
# this timestamp (process start), never "today" -- this is exactly the
# distinction Phase 16's wiring audit (Follow-up E) refused to paper over
# with a rushed JSON wrapper.
_PROCESS_STARTED_AT = datetime.now(UTC)


def render_latest_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def build_vitals_summary() -> dict:
    """A JSON-friendly summary of exactly two of this module's five real
    Prometheus metrics -- LLM token usage and broker order-dispatch
    latency -- for the Overview "System Vitals" panel, which previously
    could only say "Prometheus only" and send an operator to Grafana.

    Deliberately narrow: this reads the *same* `Counter`/`Histogram`
    objects `/metrics` already exposes (via each metric's own `.collect()`,
    not a second, independently-tracked value), so there is no risk of
    this JSON view and the Prometheus scrape ever disagreeing. Host
    CPU/memory and per-provider LLM health are NOT included here --
    neither is one of Build Spec §19's five tracked metrics, and nothing
    in this codebase computes either value anywhere; fabricating one to
    fill out this response would be exactly the "looks real, quietly
    wrong" failure mode this audit exists to catch. Those two vitals stay
    honestly labeled "not exposed" in the frontend.

    A broker with zero dispatches (`dispatch_count == 0`) reports
    `avg_latency_ms: None`, never a fabricated `0.0` -- the same
    never-fabricate-a-metric rule this codebase applies everywhere else
    (null correlations, honest partial fills, null indicator warm-up
    periods).
    """
    token_usage: list[dict] = []
    for metric_family in llm_token_usage_total.collect():
        for sample in metric_family.samples:
            if sample.name == "tradingos_llm_token_usage_total":
                token_usage.append(
                    {
                        "provider": sample.labels["provider"],
                        "token_type": sample.labels["token_type"],
                        "count": int(sample.value),
                    }
                )

    dispatch_stats: dict[str, dict[str, float]] = {}
    for metric_family in order_dispatch_latency_seconds.collect():
        for sample in metric_family.samples:
            broker = sample.labels.get("broker")
            if broker is None:
                continue
            entry = dispatch_stats.setdefault(broker, {"count": 0.0, "sum_seconds": 0.0})
            if sample.name == "tradingos_order_dispatch_latency_seconds_count":
                entry["count"] = sample.value
            elif sample.name == "tradingos_order_dispatch_latency_seconds_sum":
                entry["sum_seconds"] = sample.value

    budget_breaches: dict[str, int] = {}
    for metric_family in order_dispatch_budget_breached_total.collect():
        for sample in metric_family.samples:
            if sample.name == "tradingos_order_dispatch_budget_breached_total":
                budget_breaches[sample.labels["broker"]] = int(sample.value)

    order_dispatch: list[dict] = []
    for broker, entry in sorted(dispatch_stats.items()):
        count = int(entry["count"])
        avg_latency_ms = (entry["sum_seconds"] / count) * 1000.0 if count > 0 else None
        order_dispatch.append(
            {
                "broker": broker,
                "dispatch_count": count,
                "avg_latency_ms": round(avg_latency_ms, 2) if avg_latency_ms is not None else None,
                "budget_breaches": budget_breaches.get(broker, 0),
            }
        )

    return {
        "since": _PROCESS_STARTED_AT.isoformat(),
        "llm_token_usage": token_usage,
        "order_dispatch": order_dispatch,
    }


def _update_trading_holiday_gauge() -> None:
    today = datetime.now(UTC).date()
    trading_holiday_gauge.set(0 if is_nse_trading_day(today) else 1)


async def _trading_holiday_gauge_loop() -> None:
    while True:
        try:
            _update_trading_holiday_gauge()
        except Exception:  # noqa: BLE001 - a metrics-update failure must never crash the loop
            logger.exception("metrics.trading_holiday_gauge_update_failed")
        await asyncio.sleep(TRADING_HOLIDAY_GAUGE_UPDATE_INTERVAL_SECONDS)


def trading_holiday_gauge_updater() -> asyncio.Task:
    _update_trading_holiday_gauge()  # set an immediate value at startup, don't wait a full interval
    return asyncio.create_task(_trading_holiday_gauge_loop())
