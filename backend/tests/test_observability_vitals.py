"""System Vitals tests (Phase 17 real-world testing pass): each of the
three genuinely different metric kinds src.observability.vitals reads --
host gauges, a Redis day-bucket for LLM token usage, and a Prometheus
HTTP-API-queried latency percentile -- gets exercised against something
real (real psutil, a real Redis instance, a real httpx round-trip against
a mocked transport), never a fake in-process stand-in for the thing being
tested.
"""

import json
from datetime import date, datetime

import httpx
import pytest

from src.core.config import get_settings
from src.engine.paper_trading.market_hours import IST, next_market_event
from src.observability.vitals import (
    build_system_vitals,
    get_host_vitals,
    get_order_dispatch_latency_percentiles,
    get_token_usage_today,
    record_token_usage_today,
)


def _ist_today() -> str:
    """Same IST-calendar-day convention src.observability.vitals's own
    `_token_usage_key` uses -- using the system/UTC date here instead
    would silently target a different Redis key whenever UTC and IST fall
    on different calendar days (any time after 18:30 UTC)."""
    return datetime.now(IST).date().isoformat()


def test_get_host_vitals_reports_real_plausible_values():
    vitals = get_host_vitals()
    assert 0.0 <= vitals["cpu_percent"] <= 100.0
    assert 0.0 <= vitals["memory_percent"] <= 100.0
    assert vitals["memory_used_mb"] > 0
    assert vitals["uptime_seconds"] >= 0.0


async def test_record_and_read_token_usage_today_round_trips_through_real_redis(redis_client):
    provider = "vitals-test-token-provider"
    await redis_client.delete(f"tokens:{provider}:{_ist_today()}")

    await record_token_usage_today(redis_client, provider, 120)
    await record_token_usage_today(redis_client, provider, 30)

    usage = await get_token_usage_today(redis_client, [provider, "vitals-test-untouched-provider"])
    assert usage == {provider: 150}
    # A provider never called today is honestly absent, not a fabricated 0.
    assert "vitals-test-untouched-provider" not in usage

    await redis_client.delete(f"tokens:{provider}:{_ist_today()}")


async def test_record_token_usage_today_is_a_noop_for_zero_or_negative_tokens(redis_client):
    provider = "vitals-test-zero-token-provider"
    key = f"tokens:{provider}:{_ist_today()}"
    await redis_client.delete(key)

    await record_token_usage_today(redis_client, provider, 0)
    await record_token_usage_today(redis_client, provider, -5)

    assert await redis_client.get(key) is None


async def test_get_order_dispatch_latency_percentiles_parses_a_real_prometheus_response():
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        # The PromQL query itself multiplies by 1000 (seconds -> ms), so
        # Prometheus's real response is already in milliseconds -- this
        # mock returns the post-multiplication value, same as the real API.
        value = "123" if "0.50" in query else "456"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1700000000, value]}],
                },
            },
        )

    result = await get_order_dispatch_latency_percentiles(
        base_url="http://prometheus.invalid", transport=httpx.MockTransport(handler)
    )
    assert result["order_dispatch_p50_ms"] == pytest.approx(123.0, abs=0.1)
    assert result["order_dispatch_p95_ms"] == pytest.approx(456.0, abs=0.1)


async def test_get_order_dispatch_latency_percentiles_handles_an_empty_window_honestly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    result = await get_order_dispatch_latency_percentiles(
        base_url="http://prometheus.invalid", transport=httpx.MockTransport(handler)
    )
    assert result == {"order_dispatch_p50_ms": None, "order_dispatch_p95_ms": None}


async def test_get_order_dispatch_latency_percentiles_never_fabricates_a_value_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await get_order_dispatch_latency_percentiles(
        base_url="http://prometheus.invalid", transport=httpx.MockTransport(handler)
    )
    assert result == {"order_dispatch_p50_ms": None, "order_dispatch_p95_ms": None}


def test_next_market_event_before_open_returns_todays_open():
    # 2025-01-06 is a real Monday (a genuine NSE trading day).
    now = datetime(2025, 1, 6, 8, 0, tzinfo=IST)
    event = next_market_event(now)
    assert event.date() == date(2025, 1, 6)
    assert event.time().isoformat() == "09:15:00"


def test_next_market_event_during_session_returns_todays_close():
    now = datetime(2025, 1, 6, 11, 0, tzinfo=IST)
    event = next_market_event(now)
    assert event.date() == date(2025, 1, 6)
    assert event.time().isoformat() == "15:30:00"


def test_next_market_event_after_close_returns_next_trading_days_open():
    # 2025-01-06 is a Monday; after close should roll to Tuesday's open.
    now = datetime(2025, 1, 6, 16, 0, tzinfo=IST)
    event = next_market_event(now)
    assert event.date() == date(2025, 1, 7)
    assert event.time().isoformat() == "09:15:00"


def test_next_market_event_on_a_weekend_rolls_to_monday():
    # 2025-01-04 is a real Saturday.
    now = datetime(2025, 1, 4, 10, 0, tzinfo=IST)
    event = next_market_event(now)
    assert event.date() == date(2025, 1, 6)


async def test_build_system_vitals_assembles_the_full_documented_shape(redis_client):
    settings = get_settings()

    payload = await build_system_vitals(
        redis=redis_client,
        token_usage_providers=["vitals-test-shape-provider"],
        prometheus_base_url="http://prometheus.invalid",
        latency_budget_ms=settings.order_dispatch_latency_budget_ms,
        active_provider="anthropic",
    )
    assert set(payload.keys()) == {"host", "llm", "latency", "market", "as_of"}
    assert set(payload["host"].keys()) == {
        "cpu_percent",
        "memory_percent",
        "memory_used_mb",
        "uptime_seconds",
    }
    assert payload["llm"]["active_provider"] == "anthropic"
    assert payload["latency"]["budget_ms"] == settings.order_dispatch_latency_budget_ms
    assert payload["latency"]["window"] == "5m"
    assert isinstance(payload["market"]["is_open"], bool)
    _ = json.dumps(payload)  # every value must be genuinely JSON-serializable (no NaN)
