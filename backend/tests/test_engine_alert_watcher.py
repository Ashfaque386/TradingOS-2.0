"""The scheduled look at the Paper Desk's attention conditions
(src.orchestration.engine_alert_watcher): what it opens, what it leaves alone out of hours,
and what it never alerts on.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.brokers import feed_health
from src.engine.paper_trading.tick_feed import Tick
from src.models.engine_alert_event import EngineAlertEvent
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.observability.metrics import engine_alert_watch_last_success_timestamp_seconds
from src.observability.metrics import engine_alerts_open as open_gauge
from src.orchestration import engine_alert_watcher, engine_alerts
from src.orchestration.engine_alert_watcher import (
    broker_rejection_candidates,
    candidate_from_item,
    run_engine_alert_watch,
)
from src.orchestration.paper_attention import AttentionItem
from src.orchestration.paper_subscription_controls import SubscriptionView
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

# Monday 2 Nov 2026, 09:30 IST: the market is open. A Sunday is shut.
OPEN = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
SHUT = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
_CODE = "def run_backtest(data, config):\n    return {}\n"


@pytest.fixture(autouse=True)
def _clean_feed_health():
    feed_health.clear()
    yield
    feed_health.clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    monkeypatch.setattr(engine_alerts, "notify", fake_notify)
    return calls


async def _subscription(db_session_factory, symbol="RELIANCE", *, active=True):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"W-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = await enroll_in_paper_trading(
            db, strategy_version_id=version.id, symbol=symbol, builtin_strategy="sma_crossover"
        )
        subscription.is_active = active
        await db.commit()
        await db.refresh(subscription)
        return subscription


def _view(subscription: PaperTradingSubscription, *, state="active", reason=None, **kwargs):
    return SubscriptionView(
        subscription=subscription,
        state=state,
        state_reason=reason,
        quantity=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        costs_total=0.0,
        **kwargs,
    )


def _tick(age_seconds: float) -> dict:
    return {
        "last_tick": Tick("X", 100.0, 0, "real"),
        "last_tick_age_seconds": age_seconds,
    }


def _serve(monkeypatch, views):
    async def fake_views(db, redis, price_provider, *, now=None, **_):
        return views

    monkeypatch.setattr(engine_alert_watcher, "list_subscription_views", fake_views)


async def _watch(db_session_factory, now):
    return await run_engine_alert_watch(db_session_factory, None, None, now=now)  # type: ignore[arg-type]


async def _rows(db_session_factory):
    async with db_session_factory() as db:
        return (
            (await db.execute(select(EngineAlertEvent).order_by(EngineAlertEvent.opened_at)))
            .scalars()
            .all()
        )


async def test_a_position_nobody_is_watching_is_announced_at_once(
    db_session_factory, monkeypatch, sent
):
    switched_off = await _subscription(db_session_factory, active=False)
    _serve(monkeypatch, [_view(switched_off, state="inactive", open_legs=1)])

    result = await _watch(db_session_factory, OPEN)

    assert (result.opened, result.announced) == (1, 1)
    assert [c[1] for c in sent] == ["RELIANCE is holding a position nobody is watching"]
    (row,) = await _rows(db_session_factory)
    assert row.kind == "unmanaged"
    assert row.severity == "critical"
    assert row.subscription_id == switched_off.id
    assert row.key == f"{switched_off.id}:unmanaged"


async def test_it_is_judged_whatever_the_time(db_session_factory, monkeypatch, sent):
    switched_off = await _subscription(db_session_factory, active=False)
    _serve(monkeypatch, [_view(switched_off, state="inactive", open_legs=1)])

    result = await _watch(db_session_factory, SHUT)

    assert result.announced == 1


async def test_a_stopped_feed_is_announced_once_it_has_lasted_and_not_before(
    db_session_factory, monkeypatch, sent
):
    running = await _subscription(db_session_factory)
    _serve(
        monkeypatch,
        [_view(running, state="halted_no_data", reason="zerodha rejected the stored credentials")],
    )

    first = await _watch(db_session_factory, OPEN)
    later = await _watch(db_session_factory, OPEN + timedelta(minutes=2, seconds=1))
    again = await _watch(db_session_factory, OPEN + timedelta(minutes=5))

    assert [r.announced for r in (first, later, again)] == [0, 1, 0]
    assert [c[1] for c in sent] == ["RELIANCE is not trading"]
    assert sent[0][2] == "zerodha rejected the stored credentials"


async def test_a_warning_is_left_alone_when_the_market_is_shut(
    db_session_factory, monkeypatch, sent
):
    running = await _subscription(db_session_factory)
    _serve(monkeypatch, [_view(running, state="halted_no_data", reason="no broker is connected")])

    after_hours = await _watch(db_session_factory, SHUT)
    assert (after_hours.opened, after_hours.announced) == (0, 0)

    # It opens in the session, and the close does not clear it.
    await _watch(db_session_factory, OPEN)
    _serve(monkeypatch, [])
    await _watch(db_session_factory, SHUT + timedelta(days=1, hours=11))
    (row,) = await _rows(db_session_factory)
    assert row.resolved_at is None


async def test_a_stale_price_counts_against_the_setting_and_only_in_the_session(
    db_session_factory, monkeypatch, sent
):
    quoted = await _subscription(db_session_factory)
    _serve(monkeypatch, [_view(quoted, **_tick(130.0))])

    await _watch(db_session_factory, OPEN)
    await _watch(db_session_factory, OPEN + timedelta(minutes=3))

    assert [c[1] for c in sent] == ["RELIANCE has a stale price"]
    assert "2m ago" in sent[0][2]


async def test_a_fresh_price_is_nothing(db_session_factory, monkeypatch, sent):
    quoted = await _subscription(db_session_factory)
    _serve(monkeypatch, [_view(quoted, **_tick(119.0))])

    result = await _watch(db_session_factory, OPEN)

    assert result.opened == 0
    assert sent == []


async def test_waiting_for_history_is_shown_on_the_desk_but_never_messages_anyone(
    db_session_factory, monkeypatch, sent
):
    waiting = await _subscription(db_session_factory)
    _serve(
        monkeypatch,
        [_view(waiting, state="awaiting_history", reason="only 9 daily bars on record")],
    )

    result = await _watch(db_session_factory, OPEN)

    assert result.opened == 0
    assert await _rows(db_session_factory) == []


async def test_it_clears_when_the_position_is_looked_after_again(
    db_session_factory, monkeypatch, sent
):
    switched_off = await _subscription(db_session_factory, active=False)
    _serve(monkeypatch, [_view(switched_off, state="inactive", open_legs=1)])
    await _watch(db_session_factory, OPEN)

    _serve(monkeypatch, [_view(switched_off, state="inactive", open_legs=0)])
    result = await _watch(db_session_factory, OPEN + timedelta(minutes=1))

    assert result.resolved == 1
    assert [c[1] for c in sent] == [
        "RELIANCE is holding a position nobody is watching",
        "Cleared: RELIANCE is holding a position nobody is watching",
    ]


async def test_a_broker_refusing_the_credentials_is_one_critical_alert_until_it_stops(
    db_session_factory, monkeypatch, sent
):
    _serve(monkeypatch, [])
    feed_health.record_rejection("zerodha", status_code=403, detail="token expired")

    first = await _watch(db_session_factory, OPEN)
    second = await _watch(db_session_factory, OPEN + timedelta(minutes=1))
    feed_health.clear("zerodha")
    third = await _watch(db_session_factory, OPEN + timedelta(minutes=2))

    assert [(r.opened, r.announced, r.resolved) for r in (first, second, third)] == [
        (1, 1, 0),
        (0, 0, 0),
        (0, 0, 1),
    ]
    assert [c[1] for c in sent] == [
        "zerodha rejected the stored credentials",
        "Cleared: zerodha rejected the stored credentials",
    ]
    (row,) = await _rows(db_session_factory)
    assert (row.key, row.severity, row.kind) == (
        "broker:zerodha:rejected",
        "critical",
        "broker_rejected",
    )
    assert row.subscription_id is None


def test_the_broker_alert_says_what_is_wrong_and_what_to_do():
    feed_health.record_rejection("upstox", status_code=401, detail="invalid token")

    (candidate,) = broker_rejection_candidates()

    assert candidate.title == "upstox rejected the stored credentials"
    assert candidate.detail.startswith("HTTP 401 since ")
    assert candidate.detail.endswith(
        " IST. No prices are coming from it. Reconnect the broker in Settings > Broker."
    )


def test_no_rejection_is_no_candidate():
    assert broker_rejection_candidates() == []


def test_brokers_are_listed_in_name_order():
    feed_health.record_rejection("zerodha", status_code=403, detail="x")
    feed_health.record_rejection("upstox", status_code=401, detail="x")

    assert [c.key for c in broker_rejection_candidates()] == [
        "broker:upstox:rejected",
        "broker:zerodha:rejected",
    ]


def test_an_attention_item_becomes_a_candidate_keyed_by_its_id():
    sub_id = uuid.uuid4()
    item = AttentionItem(
        id=f"{sub_id}:halted",
        kind="halted",
        severity="warning",
        title="T is not trading",
        detail="why",
        subscription_id=str(sub_id),
        action=None,
    )

    candidate = candidate_from_item(item)

    assert (candidate.key, candidate.kind, candidate.severity) == (
        f"{sub_id}:halted",
        "halted",
        "warning",
    )
    assert (candidate.title, candidate.detail, candidate.subscription_id) == (
        "T is not trading",
        "why",
        sub_id,
    )


async def test_a_look_reports_the_open_count_and_proves_it_ran(
    db_session_factory, monkeypatch, sent
):
    switched_off = await _subscription(db_session_factory, active=False)
    running = await _subscription(db_session_factory, "TCS")
    _serve(
        monkeypatch,
        [
            _view(switched_off, state="inactive", open_legs=1),
            _view(running, state="halted_no_data", reason="no broker is connected"),
        ],
    )
    engine_alert_watch_last_success_timestamp_seconds.set(0)

    await _watch(db_session_factory, OPEN)

    assert open_gauge.labels(severity="critical")._value.get() == 1
    assert open_gauge.labels(severity="warning")._value.get() == 1
    assert open_gauge.labels(severity="info")._value.get() == 0
    assert engine_alert_watch_last_success_timestamp_seconds._value.get() > 0


async def test_the_gauges_go_back_to_zero_when_everything_clears(
    db_session_factory, monkeypatch, sent
):
    switched_off = await _subscription(db_session_factory, active=False)
    _serve(monkeypatch, [_view(switched_off, state="inactive", open_legs=1)])
    await _watch(db_session_factory, OPEN)
    _serve(monkeypatch, [])

    await _watch(db_session_factory, OPEN + timedelta(minutes=1))

    assert open_gauge.labels(severity="critical")._value.get() == 0


async def test_the_watcher_is_one_job_a_minute_that_never_overlaps_itself(db_session_factory):
    from src.orchestration.engine_alert_watcher import (
        WATCH_INTERVAL_SECONDS,
        WATCH_JOB_ID,
        start_engine_alert_watcher,
    )

    scheduler = start_engine_alert_watcher(db_session_factory, redis=None, price_provider=None)  # type: ignore[arg-type]
    try:
        [job] = scheduler.get_jobs()
        assert job.id == WATCH_JOB_ID == "engine_alert_watch"
        assert job.trigger.interval.total_seconds() == WATCH_INTERVAL_SECONDS == 60
        assert job.max_instances == 1  # a slow look is not started again on top of itself
        assert job.coalesce is True  # and a backlog of missed looks is one look
    finally:
        scheduler.shutdown(wait=False)
