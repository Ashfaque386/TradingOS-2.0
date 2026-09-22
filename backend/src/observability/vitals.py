"""System Vitals (Phase 17 real-world testing pass): the JSON summary
behind `GET /api/v1/system/vitals`, replacing Phase 16 Follow-up E's
Prometheus-Counter-derived design (the old `build_vitals_summary`, since
removed) with one that reads each figure the way its own semantics
actually require, instead of forcing every number through a Counter's
since-process-start shape:

- **Host CPU/memory** are current-value gauges -- there is no "since
  start" question for a gauge, so these are read directly via `psutil`
  against the API process's own OS view. Never derived from a Prometheus
  metric: none of the five this app tracks (src.observability.metrics)
  are host-level.
- **LLM token usage "today"** is deliberately NOT derived from
  `tradingos_llm_token_usage_total` (a real Prometheus Counter, but
  cumulative since process start, not calendar-day-scoped). Instead,
  `src.agents.llm_router` writes a second, purpose-built running total
  here on every real completion -- a Redis key per provider per IST
  calendar day (`tokens:{provider}:{YYYY-MM-DD}`), expiring after a few
  days. The Prometheus counter is untouched and keeps serving
  Grafana/long-term monitoring unchanged; this is a second, small write
  path alongside it, not a replacement.
- **Order-dispatch latency** is reported as a real rolling-window
  percentile -- `histogram_quantile(0.50 / 0.95, rate(...[5m]))` queried
  from Prometheus's own HTTP API at request time, the same query Grafana
  itself would run -- reusing the existing
  `tradingos_order_dispatch_latency_seconds` histogram
  (src.brokers.resilient.ResilientBrokerAdapter already feeds it on every
  real dispatch), never a second, parallel latency-tracking path.

IST, not UTC, is the calendar this module uses for "today" -- same
convention Phase 16 Follow-up F's "today's realized P&L" established for
an Indian-markets operator, and the same `IST` zone
`src.engine.paper_trading.market_hours` already defines.
"""

from datetime import UTC, datetime

import httpx
import psutil
import structlog
from redis.asyncio import Redis

from src.engine.paper_trading.market_hours import IST, is_market_open_ist, next_market_event

logger = structlog.get_logger(__name__)

_PROCESS_STARTED_AT = datetime.now(UTC)

# A few days, per the resolution -- long enough that a slow day's figure
# doesn't vanish before anyone looks, short enough that stale per-day keys
# don't accumulate forever in Redis.
TOKEN_USAGE_KEY_TTL_SECONDS = 3 * 24 * 3600


def _token_usage_key(provider: str, *, today: str | None = None) -> str:
    day = today if today is not None else datetime.now(IST).date().isoformat()
    return f"tokens:{provider}:{day}"


async def record_token_usage_today(redis: Redis, provider: str, tokens: int) -> None:
    """Best-effort: a Redis failure here must never break an LLM call --
    the caller (src.agents.llm_router) already recorded the same tokens
    to the real Prometheus counter regardless of what happens here."""
    if tokens <= 0:
        return
    try:
        key = _token_usage_key(provider)
        await redis.incrby(key, tokens)
        await redis.expire(key, TOKEN_USAGE_KEY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - never let a metrics write break the caller
        logger.warning("vitals.record_token_usage_failed", provider=provider, error=str(exc))


async def get_token_usage_today(redis: Redis, providers: list[str]) -> dict[str, int]:
    """Only returns entries for providers with a real, non-zero count
    today -- a provider nothing has called yet is honestly absent, never
    a fabricated `0`."""
    if not providers:
        return {}
    keys = [_token_usage_key(provider) for provider in providers]
    try:
        values = await redis.mget(keys)
    except Exception as exc:  # noqa: BLE001 - Redis unreachable -> honestly empty, not fabricated
        logger.warning("vitals.get_token_usage_failed", error=str(exc))
        return {}
    return {
        provider: int(value)
        for provider, value in zip(providers, values, strict=True)
        if value is not None
    }


def get_host_vitals() -> dict:
    memory = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": memory.percent,
        "memory_used_mb": round(memory.used / (1024 * 1024), 1),
        "uptime_seconds": round((datetime.now(UTC) - _PROCESS_STARTED_AT).total_seconds(), 1),
    }


def _parse_prometheus_scalar(payload: dict) -> float | None:
    series = payload.get("data", {}).get("result", [])
    if not series:
        return None
    raw_value = series[0].get("value", [None, None])[1]
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN -- Prometheus returns this when a window has no samples
        return None
    return value


async def get_order_dispatch_latency_percentiles(
    *,
    base_url: str,
    window: str = "5m",
    timeout: float = 3.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, float | None]:
    """Queries Prometheus's own `/api/v1/query` HTTP endpoint rather than
    re-deriving a percentile client-side from raw histogram bucket
    samples. Prometheus unreachable (not scraping yet, or -- as in this
    sandbox -- genuinely Docker-Hub-pull-blocked, see docker-compose.yml)
    reports both percentiles as `None`, never a fabricated `0.0` --
    the same real-attempt-sandbox-blocked posture every other external
    dependency in this codebase already uses."""
    queries = {
        "order_dispatch_p50_ms": (
            "histogram_quantile(0.50, "
            f"rate(tradingos_order_dispatch_latency_seconds_bucket[{window}])) * 1000"
        ),
        "order_dispatch_p95_ms": (
            "histogram_quantile(0.95, "
            f"rate(tradingos_order_dispatch_latency_seconds_bucket[{window}])) * 1000"
        ),
    }
    result: dict[str, float | None] = dict.fromkeys(queries)
    try:
        async with httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout
        ) as client:
            for key, query in queries.items():
                response = await client.get("/api/v1/query", params={"query": query})
                response.raise_for_status()
                value = _parse_prometheus_scalar(response.json())
                result[key] = round(value, 2) if value is not None else None
    except Exception as exc:  # noqa: BLE001 - Prometheus unreachable is an expected deployment state
        logger.warning("vitals.prometheus_query_failed", error=str(exc))
        result = dict.fromkeys(queries)
    return result


async def build_system_vitals(
    *,
    redis: Redis,
    token_usage_providers: list[str],
    prometheus_base_url: str,
    latency_budget_ms: float,
    active_provider: str | None,
) -> dict:
    now = datetime.now(UTC)
    token_usage_today = await get_token_usage_today(redis, token_usage_providers)
    latency = await get_order_dispatch_latency_percentiles(base_url=prometheus_base_url)
    return {
        "host": get_host_vitals(),
        "llm": {
            "active_provider": active_provider,
            "token_usage_today": token_usage_today,
        },
        "latency": {
            "order_dispatch_p50_ms": latency["order_dispatch_p50_ms"],
            "order_dispatch_p95_ms": latency["order_dispatch_p95_ms"],
            "window": "5m",
            "budget_ms": latency_budget_ms,
        },
        "market": {
            "is_open": is_market_open_ist(now),
            "next_event_at": next_market_event(now).isoformat(),
        },
        "as_of": now.isoformat(),
    }
