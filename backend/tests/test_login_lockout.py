"""The rules that lock an account after wrong passwords (src.core.login_lockout), at every boundary.

Pure functions on a small state and an explicit clock, so each edge is one line: the failure that
trips the lock, the one just short of it, the instant a lock ends, the failure that falls just
outside the window.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from src.core.login_lockout import (
    LockoutPolicy,
    SignInState,
    after_failure,
    after_success,
    is_locked,
    seconds_until_unlocked,
)

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
POLICY = LockoutPolicy(
    max_failed_attempts=5,
    failure_window=timedelta(minutes=15),
    lockout_duration=timedelta(minutes=30),
)


def _fail_n_times(n: int, start: datetime = NOW, step: timedelta = timedelta(seconds=10)):
    state = SignInState()
    moment = start
    for _ in range(n):
        state = after_failure(state, moment, POLICY)
        moment += step
    return state, moment - step


# ---- the types ----


def test_a_fresh_state_is_no_failures_and_no_lock():
    state = SignInState()

    assert (state.failed_attempts, state.last_failed_at, state.locked_until) == (0, None, None)


def test_a_state_and_a_policy_cannot_be_edited_after_they_are_made():
    # What is stored on an account is replaced by the answer of these functions, never patched.
    with pytest.raises(dataclasses.FrozenInstanceError):
        SignInState().failed_attempts = 3  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLICY.max_failed_attempts = 1000  # type: ignore[misc]


def test_a_state_and_a_policy_carry_nothing_but_their_fields():
    assert not hasattr(SignInState(), "__dict__")
    assert not hasattr(POLICY, "__dict__")


# ---- counting ----


def test_the_first_wrong_password_is_counted_and_locks_nothing():
    state = after_failure(SignInState(), NOW, POLICY)

    assert state == SignInState(failed_attempts=1, last_failed_at=NOW, locked_until=None)


def test_failures_below_the_limit_never_lock():
    state, _ = _fail_n_times(POLICY.max_failed_attempts - 1)

    assert state.failed_attempts == POLICY.max_failed_attempts - 1
    assert state.locked_until is None


def test_the_failure_that_reaches_the_limit_locks_for_the_whole_duration():
    state, last = _fail_n_times(POLICY.max_failed_attempts)

    assert state.locked_until == last + POLICY.lockout_duration
    assert state.last_failed_at == last


def test_locking_resets_the_count_so_the_next_run_starts_from_zero():
    state, _ = _fail_n_times(POLICY.max_failed_attempts)

    assert state.failed_attempts == 0


def test_a_limit_of_one_locks_on_the_first_failure():
    policy = LockoutPolicy(1, timedelta(minutes=1), timedelta(minutes=1))

    assert after_failure(SignInState(), NOW, policy).locked_until == NOW + timedelta(minutes=1)


# ---- the window ----


def test_a_failure_exactly_at_the_window_edge_still_counts_with_the_earlier_ones():
    earlier = SignInState(failed_attempts=3, last_failed_at=NOW, locked_until=None)

    state = after_failure(earlier, NOW + POLICY.failure_window, POLICY)

    assert state.failed_attempts == 4


def test_a_failure_one_second_past_the_window_starts_a_new_run():
    earlier = SignInState(failed_attempts=4, last_failed_at=NOW, locked_until=None)

    state = after_failure(earlier, NOW + POLICY.failure_window + timedelta(seconds=1), POLICY)

    assert state.failed_attempts == 1
    assert state.locked_until is None


def test_old_failures_do_not_add_up_to_a_lock_over_days():
    state = SignInState()
    for day in range(10):
        state = after_failure(state, NOW + timedelta(days=day), POLICY)

    assert state.locked_until is None
    assert state.failed_attempts == 1


# ---- the lock itself ----


def test_a_locked_account_is_locked_until_the_instant_the_lock_ends():
    locked = SignInState(0, NOW, NOW + timedelta(minutes=30))

    assert is_locked(locked, NOW)
    assert is_locked(locked, NOW + timedelta(minutes=30) - timedelta(microseconds=1))
    assert not is_locked(locked, NOW + timedelta(minutes=30))


def test_an_account_with_no_lock_is_not_locked():
    assert not is_locked(SignInState(), NOW)
    assert not is_locked(SignInState(failed_attempts=4, last_failed_at=NOW), NOW)


def test_the_wait_is_rounded_up_so_a_client_that_waits_it_out_is_let_in():
    locked = SignInState(0, NOW, NOW + timedelta(seconds=90, milliseconds=1))

    assert seconds_until_unlocked(locked, NOW) == 91


def test_the_wait_is_zero_when_not_locked_or_when_the_lock_has_ended():
    assert seconds_until_unlocked(SignInState(), NOW) == 0
    assert seconds_until_unlocked(SignInState(0, NOW, NOW), NOW) == 0
    assert seconds_until_unlocked(SignInState(0, NOW, NOW - timedelta(seconds=5)), NOW) == 0


def test_the_first_failure_after_a_lock_ran_out_is_the_first_of_a_new_run():
    ended = SignInState(0, NOW, NOW + timedelta(minutes=30))

    state = after_failure(ended, NOW + timedelta(minutes=30), POLICY)

    assert state.failed_attempts == 1
    assert state.locked_until is None


def test_a_lock_that_ran_out_does_not_carry_old_failures_into_the_next_run():
    # Stale count still in the row (a lock written by an earlier version that did not reset it).
    ended = SignInState(4, NOW, NOW + timedelta(minutes=1))

    state = after_failure(ended, NOW + timedelta(minutes=1), POLICY)

    assert state.failed_attempts == 1


# ---- success ----


def test_a_correct_password_forgets_every_failure_and_any_lock():
    state = SignInState(3, NOW, NOW + timedelta(minutes=5))

    assert after_success(state) == SignInState()


def test_a_run_interrupted_by_a_success_has_to_start_again():
    state, last = _fail_n_times(POLICY.max_failed_attempts - 1)
    state = after_success(state)
    state = after_failure(state, last + timedelta(seconds=1), POLICY)

    assert state.failed_attempts == 1
    assert state.locked_until is None
