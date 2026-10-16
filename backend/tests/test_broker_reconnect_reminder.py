"""The morning "sign your brokers in again" reminder
(src/orchestration/broker_reconnect_reminder.py).

Broker tokens die every morning (Zerodha ~06:00 IST); nothing used to tell the operator except a
card on a page they had to open. Times below are IST; `now` is always passed in, so none of this
depends on the wall clock.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.brokers import feed_health
from src.brokers.base import BrokerCredentials
from src.engine.paper_trading.market_hours import IST
from src.notifications.types import AlertLevel
from src.orchestration import broker_reconnect_reminder as rr
from src.orchestration.broker_reconnect_reminder import (
    ReconnectNeed,
    build_message,
    reconnect_needs,
    reminder_triggers,
    run_broker_reconnect_reminder_job,
)

NOW = datetime(2026, 10, 16, 8, 30, tzinfo=IST)  # a Friday morning, before the open
SECRET = "super-secret-access-token-value"


def _creds(expires_at: datetime | None, *, token: str | None = SECRET) -> BrokerCredentials:
    return BrokerCredentials(
        api_key="key",
        api_secret="secret",
        access_token=token,
        token_expires_at=expires_at.astimezone(UTC).isoformat() if expires_at else None,
    )


def _rejection(status: int = 401) -> feed_health.FeedRejection:
    return feed_health.FeedRejection(status_code=status, detail="x", at=NOW)


# ---- who needs a login ----


def test_a_token_that_expired_this_morning_needs_a_login():
    needs = reconnect_needs(NOW, {"zerodha": _creds(NOW.replace(hour=6, minute=0))})

    assert needs == [ReconnectNeed("zerodha", "the token expired at 06:00 IST")]


def test_a_token_that_died_on_an_earlier_day_names_that_day():
    """Monday's message about a token that expired on Saturday must not read like this morning."""
    monday = datetime(2026, 10, 19, 8, 30, tzinfo=IST)
    saturday_6am = datetime(2026, 10, 17, 6, 0, tzinfo=IST)

    needs = reconnect_needs(monday, {"zerodha": _creds(saturday_6am)})

    assert needs == [ReconnectNeed("zerodha", "the token expired at Sat 17 Oct 06:00 IST")]


def test_a_token_expiring_exactly_now_has_expired():
    assert reconnect_needs(NOW, {"zerodha": _creds(NOW)}) == [
        ReconnectNeed("zerodha", "the token expired at 08:30 IST")
    ]


def test_the_close_is_todays_in_ist_even_when_it_is_still_yesterday_in_utc():
    """01:00 IST is 19:30 UTC the evening before: the 15:30 close that counts is the IST day's."""
    early = datetime(2026, 10, 16, 1, 0, tzinfo=IST)

    needs = reconnect_needs(early, {"zerodha": _creds(early.replace(hour=11, minute=0))})

    assert needs == [ReconnectNeed("zerodha", "the token expires at 11:00 IST, before the close")]


def test_a_broker_with_no_token_is_not_nagged_even_if_a_stale_expiry_is_on_file():
    stale = _creds(NOW - timedelta(days=3), token=None)

    assert reconnect_needs(NOW, {"zerodha": stale}) == []


def test_a_token_good_until_tomorrow_needs_nothing():
    tomorrow = (NOW + timedelta(days=1)).replace(hour=6, minute=0)

    assert reconnect_needs(NOW, {"zerodha": _creds(tomorrow), "upstox": _creds(tomorrow)}) == []


def test_a_token_that_will_die_before_the_close_is_flagged_while_it_is_still_valid():
    """A login made yesterday evening on a short-lived token reads "valid" at 08:30 and dies at
    11:00 mid-session -- worth a nudge now, not at noon."""
    needs = reconnect_needs(NOW, {"upstox": _creds(NOW.replace(hour=11, minute=0))})

    assert needs == [ReconnectNeed("upstox", "the token expires at 11:00 IST, before the close")]


@pytest.mark.parametrize(
    ("expires", "flagged"),
    [
        (NOW.replace(hour=15, minute=30), True),  # exactly at the close: still dies in-session
        (NOW.replace(hour=15, minute=31), False),  # a minute after: survives the session
    ],
)
def test_the_close_boundary_is_inclusive(expires, flagged):
    assert bool(reconnect_needs(NOW, {"zerodha": _creds(expires)})) is flagged


def test_a_token_the_broker_rejected_needs_a_login_even_if_the_clock_says_valid():
    tomorrow = NOW + timedelta(days=1)

    needs = reconnect_needs(
        NOW, {"zerodha": _creds(tomorrow)}, {"zerodha": _rejection(403), "upstox": None}
    )

    assert needs == [ReconnectNeed("zerodha", "the broker rejected the stored token (HTTP 403)")]


def test_an_expired_token_is_reported_as_expired_even_when_a_rejection_is_also_recorded():
    needs = reconnect_needs(
        NOW, {"zerodha": _creds(NOW.replace(hour=6, minute=0))}, {"zerodha": _rejection()}
    )

    assert needs[0].reason == "the token expired at 06:00 IST"


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"zerodha": None, "upstox": None},
        {
            "zerodha": _creds(None, token=None)
        },  # keys saved, never connected: a choice, not an expiry
        {"zerodha": _creds(None)},  # a token with no known expiry reads "valid"
    ],
)
def test_brokers_that_are_not_connected_or_have_no_known_expiry_are_left_alone(credentials):
    assert reconnect_needs(NOW, credentials) == []


def test_an_unparseable_expiry_is_not_a_reason_to_nag():
    creds = BrokerCredentials(api_key="k", access_token=SECRET, token_expires_at="not a date")

    assert reconnect_needs(NOW, {"zerodha": creds}) == []


def test_a_naive_expiry_is_read_as_utc():
    expired = BrokerCredentials(
        api_key="k",
        access_token=SECRET,
        token_expires_at="2026-10-16T00:30:00",  # 06:00 IST
    )

    assert reconnect_needs(NOW, {"zerodha": expired}) == [
        ReconnectNeed("zerodha", "the token expired at 06:00 IST")
    ]


def test_brokers_are_reported_in_the_order_the_app_knows_them():
    both = _creds(NOW.replace(hour=6, minute=0))

    needs = reconnect_needs(NOW, {"upstox": both, "zerodha": both})

    assert [n.broker for n in needs] == ["zerodha", "upstox"]


# ---- the message ----

TWO = [ReconnectNeed("zerodha", "the token expired at 06:00 IST"), ReconnectNeed("upstox", "x")]


def test_before_the_open_the_message_counts_the_minutes_and_names_each_broker():
    title, body = build_message(TWO, NOW, settings_url="http://localhost:3000/settings")

    assert title == "Broker sign-in needed before the market opens"
    assert "Zerodha: the token expired at 06:00 IST." in body
    assert "Upstox: x." in body
    assert "opens at 09:15 IST (45 min)" in body
    assert body.endswith("Reconnect: http://localhost:3000/settings")


def test_after_the_open_the_message_says_so():
    title, body = build_message(TWO, NOW.replace(hour=9, minute=40), settings_url=None)

    assert title == "Broker sign-in needed -- the market is open"
    assert "already open" in body and "opens at" not in body
    assert body.endswith("Reconnect: Settings > Broker Config")


def test_the_second_reminder_is_a_quarter_of_an_hour_before_the_open():
    _, body = build_message(TWO, NOW.replace(hour=9, minute=0), settings_url=None)

    assert "(15 min)" in body


def test_no_credential_ever_reaches_the_message():
    creds = _creds(NOW.replace(hour=6, minute=0))
    needs = reconnect_needs(NOW, {"zerodha": creds})

    title, body = build_message(needs, NOW, settings_url=None)

    text = (title + body).lower()
    assert SECRET not in title + body
    assert "secret" not in text  # neither the token above nor the stored api_secret


# ---- the job ----


class _Store:
    def __init__(self, credentials):
        self._credentials = credentials

    def get_credentials(self, broker):
        return self._credentials.get(broker)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append({"level": level, "title": title, "body": body, "details": details})
        return []

    monkeypatch.setattr(rr, "notify", fake_notify)
    monkeypatch.setattr(rr, "is_nse_trading_day", lambda day: True)
    monkeypatch.setattr(rr, "_settings_url", lambda: "http://localhost:3000/settings")
    return calls


def _store(monkeypatch, credentials):
    monkeypatch.setattr(rr, "get_secrets_store", lambda: _Store(credentials))


async def test_an_expired_token_sends_one_daily_level_message(monkeypatch, sent):
    _store(monkeypatch, {"zerodha": _creds(NOW.replace(hour=6, minute=0))})

    needs = await run_broker_reconnect_reminder_job(NOW)

    assert [n.broker for n in needs] == ["zerodha"]
    assert len(sent) == 1
    assert sent[0]["level"] == AlertLevel.DAILY
    assert sent[0]["details"] == {"brokers": "zerodha"}
    assert "Zerodha" in sent[0]["body"] and SECRET not in sent[0]["body"]


async def test_nothing_to_report_sends_nothing(monkeypatch, sent):
    tomorrow = (NOW + timedelta(days=1)).replace(hour=6, minute=0)
    _store(monkeypatch, {"zerodha": _creds(tomorrow), "upstox": _creds(tomorrow)})

    assert await run_broker_reconnect_reminder_job(NOW) == []
    assert sent == []


async def test_a_market_holiday_sends_nothing_even_with_an_expired_token(monkeypatch, sent):
    monkeypatch.setattr(rr, "is_nse_trading_day", lambda day: False)
    _store(monkeypatch, {"zerodha": _creds(NOW.replace(hour=6, minute=0))})

    assert await run_broker_reconnect_reminder_job(NOW) == []
    assert sent == []


async def test_the_trading_day_is_judged_in_ist_not_utc(monkeypatch, sent):
    """Sunday 19:30 UTC is already Monday 01:00 IST: the day that counts is the IST one."""
    asked = []
    monkeypatch.setattr(rr, "is_nse_trading_day", lambda day: asked.append(day) or True)
    _store(monkeypatch, {})

    await run_broker_reconnect_reminder_job(
        datetime(2026, 10, 18, 19, 30, tzinfo=UTC)
    )  # Mon 01:00 IST

    assert [d.isoformat() for d in asked] == ["2026-10-19"]


async def test_no_secrets_store_means_no_reminder_and_no_crash(monkeypatch, sent):
    from src.security.secrets_store import SecretsStoreError

    def boom():
        raise SecretsStoreError("no key")

    monkeypatch.setattr(rr, "get_secrets_store", boom)

    assert await run_broker_reconnect_reminder_job(NOW) == []
    assert sent == []


async def test_a_recorded_rejection_is_picked_up_by_the_job(monkeypatch, sent):
    tomorrow = (NOW + timedelta(days=1)).replace(hour=6, minute=0)
    _store(monkeypatch, {"zerodha": _creds(tomorrow)})
    feed_health.record_rejection("zerodha", status_code=401, detail="refused")

    needs = await run_broker_reconnect_reminder_job(NOW)

    assert needs == [ReconnectNeed("zerodha", "the broker rejected the stored token (HTTP 401)")]


# ---- the schedule ----


def test_there_are_two_weekday_reminders_at_0830_and_0900_ist():
    triggers = dict(reminder_triggers())

    assert sorted(triggers) == [
        "notifications_broker_reconnect_0830",
        "notifications_broker_reconnect_0900",
    ]
    friday_early = datetime(2026, 10, 16, 0, 0, tzinfo=IST)
    first = triggers["notifications_broker_reconnect_0830"].get_next_fire_time(None, friday_early)
    second = triggers["notifications_broker_reconnect_0900"].get_next_fire_time(None, friday_early)
    assert first == datetime(2026, 10, 16, 8, 30, tzinfo=IST)
    assert second == datetime(2026, 10, 16, 9, 0, tzinfo=IST)


def test_the_reminders_skip_the_weekend():
    trigger = dict(reminder_triggers())["notifications_broker_reconnect_0830"]
    saturday = datetime(2026, 10, 17, 0, 0, tzinfo=IST)

    assert trigger.get_next_fire_time(None, saturday) == datetime(2026, 10, 19, 8, 30, tzinfo=IST)


async def test_the_notification_scheduler_registers_both_reminders_beside_the_daily_summary():
    from src.orchestration.notification_scheduler import (
        DAILY_SUMMARY_JOB_ID,
        start_notification_scheduler,
    )

    scheduler = start_notification_scheduler(None)  # type: ignore[arg-type]
    try:
        ids = {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)

    assert ids == {
        DAILY_SUMMARY_JOB_ID,
        "notifications_broker_reconnect_0830",
        "notifications_broker_reconnect_0900",
    }
