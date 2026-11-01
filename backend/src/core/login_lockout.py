"""When a run of wrong passwords locks an account, and for how long.

Pure rules on a small state, no database and no clock of its own: the sign-in route reads an
account's state, asks these functions what it becomes, and stores the answer. Keeping the decision
here means it can be tested at every boundary (the failure that trips the lock, the failure just
outside the window, the instant a lock ends) without a request.

The count is per account, not per address: an attacker who spreads guesses over many addresses still
meets the lock. The cost is that anyone who knows an email address can lock that account for a
while; that is accepted, and an administrator can unlock it sooner (`POST
/admin/users/{id}/unlock`).
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class LockoutPolicy:
    max_failed_attempts: int
    failure_window: timedelta
    lockout_duration: timedelta


@dataclass(frozen=True, slots=True)
class SignInState:
    failed_attempts: int = 0
    last_failed_at: datetime | None = None
    locked_until: datetime | None = None


def is_locked(state: SignInState, now: datetime) -> bool:
    return state.locked_until is not None and state.locked_until > now


def seconds_until_unlocked(state: SignInState, now: datetime) -> int:
    """Whole seconds left on the lock, rounded up so a client that waits this long is not told no
    again; 0 when the account is not locked."""
    if state.locked_until is None or state.locked_until <= now:
        return 0
    return math.ceil((state.locked_until - now).total_seconds())


def after_failure(state: SignInState, now: datetime, policy: LockoutPolicy) -> SignInState:
    """The state after one more wrong password.

    A failure counts on top of the earlier ones only while they are recent: once the window since
    the last failure has passed, or a lock has run out, it is the first of a new run. The failure
    that reaches the limit locks the account and starts the count again from zero for when it opens.
    """
    lock_ran_out = state.locked_until is not None and state.locked_until <= now
    run_is_stale = (
        state.last_failed_at is None or now - state.last_failed_at > policy.failure_window
    )
    previous = 0 if (lock_ran_out or run_is_stale) else state.failed_attempts

    attempts = previous + 1
    if attempts >= policy.max_failed_attempts:
        return SignInState(
            failed_attempts=0, last_failed_at=now, locked_until=now + policy.lockout_duration
        )
    return SignInState(failed_attempts=attempts, last_failed_at=now, locked_until=None)


def after_success(_state: SignInState) -> SignInState:
    """A correct password forgets the failures."""
    return SignInState()
