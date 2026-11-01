"""A live order is priced off a fresh quote from the broker, and that quote is judged before a real
order is placed on it: its exchange stamp must not be from a previous day, and the circuit band is
checked against the exchange's own limits or, failing those, the previous close -- not against the
quote's own price, which can never be outside its own band.
"""

import re
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from src.core.config import get_settings
from src.models.audit_log import AuditLog
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.observability.metrics import paper_quotes_refused_exchange_stale_total as refused_counter
from src.orchestration.live_trading import generate_live_order_intent
from tests.test_orchestration_live_trading import (
    _REGULATORY_PROVIDER,
    _enroll_and_enable_autonomy,
    _make_live_eligible_strategy,
    _mock_adapter,
)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Broker:
    """A Zerodha-shaped quote endpoint that records the orders sent to it."""

    def __init__(self, price, *, stamp=None, lower=None, upper=None):
        row = {"last_price": price, "depth": {}}
        if stamp is not None:
            row["timestamp"] = stamp.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        if lower is not None:
            row["lower_circuit_limit"] = lower
            row["upper_circuit_limit"] = upper
        self.row = row
        self.orders = 0

    def adapter(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/quote":
                return httpx.Response(200, json={"data": {"DEMOSTOCK": self.row}})
            self.orders += 1
            return httpx.Response(200, json={"data": {"order_id": "OID"}})

        return _mock_adapter(handler)


async def _subscription(db_session_factory, *, previous_close=100.0, signal="BUY"):
    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        row = await db.get(LiveTradingSubscription, subscription.id)
        row.last_signal_type = signal
        row.last_signal_reference_price = previous_close
        await db.commit()
    return subscription, strategy_id


async def _generate(db_session_factory, subscription, broker, *, tick_price=100.0):
    async with db_session_factory() as db:
        return await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=tick_price,
            adapter=broker.adapter(),
            regulatory_provider=_REGULATORY_PROVIDER,
        )


async def _audit(db_session_factory, action):
    async with db_session_factory() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


def _fresh():
    return datetime.now(UTC) - timedelta(seconds=3)


# ---- the exchange stamp ----


async def test_a_quote_stamped_on_a_previous_day_is_not_traded_on(db_session_factory):
    subscription, _ = await _subscription(db_session_factory)
    broker = _Broker(100.0, stamp=datetime.now(UTC) - timedelta(days=1, hours=3))
    before = refused_counter.labels(broker="zerodha")._value.get()

    intent = await _generate(db_session_factory, subscription, broker)

    assert intent.status == "failed"
    assert broker.orders == 0  # nothing reached the broker
    [entry] = await _audit(db_session_factory, "live_order_intent.submission_failed")
    assert entry.details["reason"].startswith("quote refused: the exchange stamp (")
    assert entry.details["reason"].endswith("IST) is from a previous day")
    assert refused_counter.labels(broker="zerodha")._value.get() == before + 1


async def test_a_quote_from_today_is_traded_on(db_session_factory):
    subscription, _ = await _subscription(db_session_factory)
    broker = _Broker(100.0, stamp=_fresh())

    intent = await _generate(db_session_factory, subscription, broker)

    assert intent.status == "submitted"
    assert broker.orders == 1


async def test_a_quote_with_no_stamp_is_not_refused_on_that_account(db_session_factory):
    subscription, _ = await _subscription(db_session_factory)

    intent = await _generate(db_session_factory, subscription, _Broker(100.0))

    assert intent.status == "submitted"


async def test_the_optional_age_limit_applies_to_a_live_order_too(db_session_factory, monkeypatch):
    monkeypatch.setenv("PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS", "30")
    get_settings.cache_clear()
    subscription, _ = await _subscription(db_session_factory)

    intent = await _generate(
        db_session_factory,
        subscription,
        _Broker(100.0, stamp=datetime.now(UTC) - timedelta(seconds=45)),
    )

    assert intent.status == "failed"
    [entry] = await _audit(db_session_factory, "live_order_intent.submission_failed")
    assert re.search(r"is 4\d+s old \(the limit is 30s\)", entry.details["reason"])


# ---- the circuit band ----


async def test_the_exchanges_limits_in_the_quote_are_what_the_order_is_checked_against(
    db_session_factory,
):
    subscription, _ = await _subscription(db_session_factory)

    refused = await _generate(
        db_session_factory, subscription, _Broker(110.0, stamp=_fresh(), lower=90.0, upper=110.0)
    )
    assert refused.status == "failed"
    [failure] = await _audit(db_session_factory, "live_order_intent.submission_failed")
    assert "at/above the exchange's upper circuit limit 110.00" in failure.details["reason"]


async def test_inside_the_exchanges_limits_the_order_goes_and_the_audit_says_what_judged_it(
    db_session_factory,
):
    subscription, _ = await _subscription(db_session_factory, previous_close=60.0)  # far away

    intent = await _generate(
        db_session_factory, subscription, _Broker(100.0, stamp=_fresh(), lower=90.0, upper=110.0)
    )

    assert intent.status == "submitted"
    [entry] = await _audit(db_session_factory, "live_order_intent.submitted")
    assert entry.details["circuit_band"] == "exchange limits"
    assert entry.details["reference_price"] == 100.0


async def test_without_exchange_limits_the_band_is_measured_from_the_previous_close(
    db_session_factory,
):
    subscription, _ = await _subscription(db_session_factory, previous_close=70.0)  # 43% below

    intent = await _generate(db_session_factory, subscription, _Broker(100.0, stamp=_fresh()))

    assert intent.status == "failed"
    [entry] = await _audit(db_session_factory, "live_order_intent.submission_failed")
    assert "reference table's circuit band 20.00%" in entry.details["reason"]
    assert "from the reference price" in entry.details["reason"]


async def test_a_price_near_the_previous_close_goes_and_is_recorded_as_judged_by_it(
    db_session_factory,
):
    subscription, _ = await _subscription(db_session_factory, previous_close=98.0)

    intent = await _generate(db_session_factory, subscription, _Broker(100.0, stamp=_fresh()))

    assert intent.status == "submitted"
    [entry] = await _audit(db_session_factory, "live_order_intent.submitted")
    assert entry.details["circuit_band"] == "previous close"


async def test_with_neither_limits_nor_a_previous_close_the_band_cannot_be_judged(
    db_session_factory,
):
    subscription, _ = await _subscription(db_session_factory, previous_close=None)
    broker = _Broker(100.0, stamp=_fresh())

    intent = await _generate(db_session_factory, subscription, broker)

    assert intent.status == "failed"
    assert broker.orders == 0
    [entry] = await _audit(db_session_factory, "live_order_intent.submission_failed")
    assert entry.details["reason"] == (
        "the circuit band cannot be judged: the quote carried no exchange circuit limits and "
        "there is no previous close to measure the reference table's band from"
    )


# ---- a stop-loss exit ----


async def _holding(db_session_factory, strategy_id, *, cost=100.0, quantity=10):
    async with db_session_factory() as db:
        db.add(
            LivePosition(
                strategy_id=strategy_id, symbol="DEMOSTOCK", quantity=quantity, avg_cost=cost
            )
        )
        await db.commit()


async def test_a_stop_loss_exit_goes_even_when_the_price_has_gapped_past_the_band(
    db_session_factory,
):
    subscription, strategy_id = await _subscription(
        db_session_factory, previous_close=100.0, signal=None
    )
    await _holding(db_session_factory, strategy_id)
    broker = _Broker(60.0, stamp=_fresh())  # 40% down: past the 20% table band

    intent = await _generate(db_session_factory, subscription, broker, tick_price=60.0)

    assert intent.intent_type == "stop"
    assert intent.status == "submitted"
    assert broker.orders == 1
    [entry] = await _audit(db_session_factory, "live_order_intent.submitted")
    assert entry.details["circuit_band"] == "skipped (stop-loss exit)"


async def test_a_stop_loss_exit_needs_no_previous_close(db_session_factory):
    subscription, strategy_id = await _subscription(
        db_session_factory, previous_close=None, signal=None
    )
    await _holding(db_session_factory, strategy_id)

    intent = await _generate(
        db_session_factory, subscription, _Broker(60.0, stamp=_fresh()), tick_price=60.0
    )

    assert intent.intent_type == "stop"
    assert intent.status == "submitted"


async def test_a_stop_loss_exit_is_still_refused_on_a_stale_quote(db_session_factory):
    subscription, strategy_id = await _subscription(db_session_factory, signal=None)
    await _holding(db_session_factory, strategy_id)
    broker = _Broker(60.0, stamp=datetime.now(UTC) - timedelta(days=2))

    intent = await _generate(db_session_factory, subscription, broker, tick_price=60.0)

    assert intent.status == "failed"  # an old price is no better a basis for an exit
    assert broker.orders == 0


async def test_no_intents_beyond_the_expected_are_left_behind(db_session_factory):
    subscription, _ = await _subscription(db_session_factory, previous_close=70.0)

    await _generate(db_session_factory, subscription, _Broker(100.0, stamp=_fresh()))

    async with db_session_factory() as db:
        rows = (await db.execute(select(LiveOrderIntent))).scalars().all()
    assert [r.status for r in rows] == ["failed"]


# ---- a live order that was decided and not sent is an engine alert ----


async def _alerts(db_session_factory):
    from src.models.engine_alert_event import EngineAlertEvent

    async with db_session_factory() as db:
        return (await db.execute(select(EngineAlertEvent))).scalars().all()


async def test_a_live_order_that_could_not_be_sent_is_an_engine_alert_with_the_reason(
    db_session_factory,
):
    from src.orchestration import engine_alerts

    subscription, _ = await _subscription(db_session_factory)
    broker = _Broker(100.0, stamp=datetime.now(UTC) - timedelta(days=1, hours=3))

    intent = await _generate(db_session_factory, subscription, broker)
    await engine_alerts.wait_for_announcements()

    [alert] = await _alerts(db_session_factory)
    assert intent.status == "failed"
    assert (alert.source, alert.kind, alert.severity) == ("event", "live_intent_failed", "warning")
    assert alert.key == f"live_intent_failed:{intent.id}"
    assert alert.title == "Live buy of DEMOSTOCK was not sent"
    assert alert.detail.startswith("quote refused: the exchange stamp (")
    assert alert.resolved_at == alert.opened_at  # something that happened, not a standing condition


async def test_a_stop_loss_exit_that_could_not_be_sent_is_critical(db_session_factory):
    from src.orchestration import engine_alerts

    subscription, strategy_id = await _subscription(db_session_factory, signal=None)
    await _holding(db_session_factory, strategy_id)
    broker = _Broker(60.0, stamp=datetime.now(UTC) - timedelta(days=2))

    await _generate(db_session_factory, subscription, broker, tick_price=60.0)
    await engine_alerts.wait_for_announcements()

    [alert] = await _alerts(db_session_factory)
    assert alert.severity == "critical"
    assert alert.title == "Live sell of DEMOSTOCK was not sent (a stop-loss exit)"


async def test_an_order_that_was_sent_raises_no_alert(db_session_factory):
    from src.orchestration import engine_alerts

    subscription, _ = await _subscription(db_session_factory)

    intent = await _generate(db_session_factory, subscription, _Broker(100.0, stamp=_fresh()))
    await engine_alerts.wait_for_announcements()

    assert intent.status == "submitted"
    assert await _alerts(db_session_factory) == []
