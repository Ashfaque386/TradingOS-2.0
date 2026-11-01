"""Is the exchange's own stamp on a quote too old to act on? (src.engine.paper_trading.quote_guard)

IST is UTC+5:30, so Monday 2 Nov 2026 09:30 IST is 04:00 UTC. The rule: refuse a stamp from an
earlier IST date; optionally refuse one older than a limit; let through what cannot be judged.
"""

from datetime import UTC, datetime, timedelta, timezone

from src.engine.paper_trading.quote_guard import EXCHANGE_CLOCK_SKEW_SECONDS, exchange_stamp_problem

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)  # Monday 09:30 IST


def problem(stamp, *, now=NOW, max_age=0.0):
    return exchange_stamp_problem(stamp, now=now, max_age_seconds=max_age)


def test_a_quote_with_no_stamp_is_let_through_because_there_is_nothing_to_judge():
    assert problem(None) is None
    assert problem(None, max_age=30.0) is None


def test_a_stamp_from_a_few_seconds_ago_is_fine():
    assert problem(NOW - timedelta(seconds=5)) is None


def test_a_stamp_from_the_previous_trading_day_is_refused_and_says_when_it_was():
    friday_close = datetime(2026, 10, 30, 15, 29, tzinfo=IST)

    assert problem(friday_close) == (
        "the exchange stamp (2026-10-30 15:29 IST) is from a previous day"
    )


def test_the_day_is_the_indian_day_not_the_utc_day():
    # 00:00 IST Monday is 18:30 UTC *Sunday*: still Monday's session by the exchange's clock.
    monday_midnight = datetime(2026, 11, 2, 0, 0, tzinfo=IST)
    sunday_late = datetime(2026, 11, 1, 23, 59, 59, tzinfo=IST)

    assert problem(monday_midnight) is None
    assert problem(sunday_late) == (
        "the exchange stamp (2026-11-01 23:59 IST) is from a previous day"
    )


def test_just_after_midnight_the_previous_evening_is_already_yesterday():
    now = datetime(2026, 11, 2, 0, 10, tzinfo=IST)

    assert problem(datetime(2026, 11, 1, 23, 59, tzinfo=IST), now=now) is not None
    assert problem(datetime(2026, 11, 2, 0, 5, tzinfo=IST), now=now) is None


def test_a_stamp_in_another_timezone_is_read_correctly():
    stamped_in_utc = datetime(2026, 10, 30, 9, 59, tzinfo=UTC)  # 15:29 IST on the 30th

    assert problem(stamped_in_utc) == (
        "the exchange stamp (2026-10-30 15:29 IST) is from a previous day"
    )


def test_a_stamp_in_the_future_is_let_through_not_trusted_either_way():
    assert problem(NOW + timedelta(seconds=30)) is None
    assert problem(NOW + timedelta(days=2), max_age=10.0) is None


def test_a_stamp_a_little_ahead_of_our_clock_is_skew_and_judged_as_now():
    assert EXCHANGE_CLOCK_SKEW_SECONDS == 2.0
    assert problem(NOW + timedelta(seconds=2)) is None  # on the limit of skew
    assert problem(NOW + timedelta(seconds=2), max_age=10.0) is None


def test_with_no_age_limit_an_old_stamp_from_today_is_fine_a_quiet_stock_is_not_a_closed_market():
    three_hours_ago = NOW - timedelta(hours=3)  # 06:30 IST, still Monday

    assert problem(three_hours_ago) is None
    assert problem(three_hours_ago, max_age=0.0) is None
    assert problem(three_hours_ago, max_age=-5.0) is None  # a negative limit is no limit


def test_an_age_limit_refuses_what_is_older_and_says_by_how_much():
    assert problem(NOW - timedelta(seconds=61), max_age=60.0) == (
        "the exchange stamp is 61s old (the limit is 60s)"
    )


def test_a_stamp_exactly_at_the_limit_is_accepted():
    assert problem(NOW - timedelta(seconds=60), max_age=60.0) is None
    assert problem(NOW - timedelta(seconds=59), max_age=60.0) is None


def test_a_previous_day_is_reported_as_that_even_when_an_age_limit_is_set():
    yesterday = datetime(2026, 11, 1, 12, 0, tzinfo=IST)

    assert problem(yesterday, max_age=60.0) == (
        "the exchange stamp (2026-11-01 12:00 IST) is from a previous day"
    )


def test_the_age_limit_is_off_when_not_given():
    # Called with no limit at all: a stamp half a minute old is fine.
    assert exchange_stamp_problem(NOW - timedelta(seconds=30), now=NOW) is None


def test_any_positive_age_limit_applies_even_a_fraction_of_a_second():
    refused = problem(NOW - timedelta(seconds=10), max_age=0.5)

    assert refused is not None and refused.startswith("the exchange stamp is 10s old")


def test_a_stamp_from_before_midnight_a_moment_ago_is_still_yesterdays():
    # 00:00:01 IST Monday; the stamp is 1.5 seconds old, and from Sunday.
    now = datetime(2026, 11, 2, 0, 0, 1, tzinfo=IST)
    stamp = datetime(2026, 11, 1, 23, 59, 59, 500_000, tzinfo=IST)

    assert (
        problem(stamp, now=now)
        == "the exchange stamp (2026-11-01 23:59 IST) is from a previous day"
    )
