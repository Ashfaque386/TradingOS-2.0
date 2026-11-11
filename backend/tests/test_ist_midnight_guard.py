"""The guard that keeps a test from running across midnight IST (tests/ist_midnight.py).

Midnight IST is 18:30 UTC.
"""

from datetime import UTC, datetime

import pytest

from tests.ist_midnight import MARGIN_SECONDS, seconds_to_wait


def _utc(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 11, 11, hour, minute, second, micro, tzinfo=UTC)


@pytest.mark.parametrize(
    "now",
    [
        _utc(9, 0),
        _utc(18, 28, 59),  # 61 s before midnight IST: just outside the margin
        _utc(18, 30, 0),  # midnight itself: the new day has begun
        _utc(18, 30, 30),
    ],
)
def test_far_enough_from_midnight_ist_it_does_not_wait(now):
    assert seconds_to_wait(now) == 0.0


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_utc(18, 29, 0), MARGIN_SECONDS + 1.0),  # exactly the margin away counts as inside it
        (_utc(18, 29, 30), 31.0),
        (_utc(18, 29, 59, 500_000), 1.5),
        (_utc(18, 29, 1), 60.0),
    ],
)
def test_inside_the_last_minute_it_waits_until_just_past_midnight_ist(now, expected):
    assert seconds_to_wait(now) == pytest.approx(expected)


def test_the_margin_can_be_widened():
    assert seconds_to_wait(_utc(18, 25), margin_seconds=600) == pytest.approx(301.0)
    assert seconds_to_wait(_utc(18, 25)) == 0.0


def test_a_naive_instant_is_refused():
    with pytest.raises(ValueError):
        seconds_to_wait(datetime(2026, 11, 11, 18, 29, 30))
