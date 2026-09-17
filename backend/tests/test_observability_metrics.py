"""Prometheus metrics smoke tests (Build Spec §19, item 4): exposition
format, presence of all five documented metrics, and the trading-holiday
gauge's real (never-fabricated) value against src.data.nse_calendar.
"""

from datetime import UTC, datetime

from src.data.nse_calendar import is_nse_trading_day
from src.observability.metrics import (
    _update_trading_holiday_gauge,
    render_latest_metrics,
    trading_holiday_gauge,
)


async def test_metrics_endpoint_is_unauthenticated_and_returns_text_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


async def test_metrics_endpoint_exposes_all_five_documented_metrics(client):
    resp = await client.get("/metrics")
    body = resp.text
    assert "tradingos_ws_latency_seconds" in body
    assert "tradingos_agent_node_duration_seconds" in body
    assert "tradingos_order_dispatch_latency_seconds" in body
    assert "tradingos_order_dispatch_budget_breached_total" in body
    assert "tradingos_llm_token_usage_total" in body
    assert "tradingos_trading_holiday" in body


def test_render_latest_metrics_returns_bytes_and_prometheus_content_type():
    body, content_type = render_latest_metrics()
    assert isinstance(body, bytes)
    assert "text/plain" in content_type


def test_trading_holiday_gauge_reflects_the_real_nse_calendar():
    _update_trading_holiday_gauge()
    today = datetime.now(UTC).date()
    expected = 0.0 if is_nse_trading_day(today) else 1.0
    assert trading_holiday_gauge._value.get() == expected
