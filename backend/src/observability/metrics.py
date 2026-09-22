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


def render_latest_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


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
