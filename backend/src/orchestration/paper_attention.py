"""What on the Paper Desk needs a person, worked out on the server.

The console has always worked this out in the browser (`attentionItems` in lib/paper-trading.ts),
so a position nobody was watching was only ever noticed by someone looking at the page. This is the
same rules in Python, for the engine-alert watcher (src.orchestration.engine_alert_watcher) to tell
a person who is not.

The two must say the same thing: the same items, with the same ids, severities, titles and
details. They are pinned to each other by one shared fixture, e2e/fixtures/paper-attention.json,
which the console's logic spec (e2e/paper-trading-logic.spec.ts) and this module's test
(tests/test_paper_attention.py) both run. Change a rule in one place and the other fails until
it follows.

An item's id is `<subscription id>:<kind>`; the watcher uses it as the alert's key, so one
condition on one subscription is one alert for as long as it lasts.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from src.orchestration.paper_subscription_controls import SubscriptionView

Severity = Literal["critical", "warning", "info"]
Kind = Literal["unmanaged", "halted", "history", "unpriced", "refused", "stale"]
Action = Literal["close"]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}

_OPTION_STRATEGY_LABEL = {"long_option": "long option", "vertical_spread": "credit spread"}


@dataclass(frozen=True, slots=True)
class AttentionSubject:
    """The fields of one subscription the rules look at (what the console's list carries)."""

    id: str
    symbol: str
    instrument_kind: str
    option_strategy: str | None
    is_active: bool
    managed: bool
    state: str
    state_reason: str | None
    open_legs: int
    unrealized_pnl: float | None
    # Seconds since the newest price arrived; None when none ever has.
    last_tick_age_seconds: float | None
    # Set while this subscription's quotes are being refused (their exchange stamp is from a
    # previous day, or too old): which broker said it and why.
    refusal_broker: str | None = None
    refusal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AttentionItem:
    id: str
    kind: Kind
    severity: Severity
    title: str
    detail: str
    subscription_id: str
    action: Action | None


def subject_from_view(view: SubscriptionView) -> AttentionSubject:
    sub = view.subscription
    return AttentionSubject(
        id=str(sub.id),
        symbol=sub.symbol,
        instrument_kind=sub.instrument_kind,
        option_strategy=sub.option_strategy,
        is_active=sub.is_active,
        managed=view.managed,
        state=view.state,
        state_reason=view.state_reason,
        open_legs=view.open_legs,
        unrealized_pnl=view.unrealized_pnl,
        last_tick_age_seconds=view.last_tick_age_seconds if view.last_tick is not None else None,
        refusal_broker=view.quote_refusal.broker if view.quote_refusal is not None else None,
        refusal_reason=view.quote_refusal.reason if view.quote_refusal is not None else None,
    )


def subscription_name(subject: AttentionSubject) -> str:
    if subject.instrument_kind == "option" and subject.option_strategy:
        label = _OPTION_STRATEGY_LABEL.get(subject.option_strategy, subject.option_strategy)
        return f"{subject.symbol} {label}"
    return f"{subject.symbol} future" if subject.instrument_kind == "future" else subject.symbol


def _round_half_up(value: float) -> int:
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_age(seconds: float | None) -> str:
    """The console's `formatAge`, digit for digit (JavaScript rounds a half up, Python's own
    `round` goes to the even number, so neither `round` nor `format` is used here)."""
    if seconds is None:
        return "never"
    if seconds < 1:
        return "just now"
    if seconds < 60:
        return f"{_round_half_up(seconds)}s ago"
    if seconds < 3600:
        return f"{_round_half_up(seconds / 60)}m ago"
    if seconds < 86400:
        hours = Decimal(seconds / 3600).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{hours}h ago"
    return f"{_round_half_up(seconds / 86400)}d ago"


def attention_items(
    subjects: Iterable[AttentionSubject], *, market_open: bool, stale_after_seconds: float
) -> list[AttentionItem]:
    """Everything an operator should look at, most urgent first. `market_open` decides whether a
    price that has stopped moving is a problem (a shut market is not)."""
    items: list[AttentionItem] = []
    for sub in subjects:
        name = subscription_name(sub)

        def add(
            kind: Kind,
            severity: Severity,
            title: str,
            detail: str,
            action: Action | None = None,
            *,
            sub: AttentionSubject = sub,
        ) -> None:
            items.append(
                AttentionItem(f"{sub.id}:{kind}", kind, severity, title, detail, sub.id, action)
            )

        if not sub.managed:
            add(
                "unmanaged",
                "critical",
                f"{name} is holding a position nobody is watching",
                "The subscription is switched off, so its stop-loss no longer runs. Close the "
                "position, or switch the subscription back on.",
                "close",
            )
        if sub.is_active and sub.state == "halted_no_data":
            add(
                "halted",
                "warning",
                f"{name} is not trading",
                sub.state_reason or "There is no real price feed for it.",
            )
        if sub.is_active and sub.state == "awaiting_history":
            add(
                "history",
                "info",
                f"{name} is waiting for price history",
                sub.state_reason or "It needs more real price history before it can signal.",
            )
        if (
            sub.is_active
            and sub.state == "active"
            and sub.open_legs > 0
            and sub.unrealized_pnl is None
        ):
            add(
                "unpriced",
                "warning",
                f"{name} has a position with no price",
                "At least one open leg has never been priced, so its value and stop-loss are "
                "unknown.",
            )
        if sub.is_active and sub.state == "active" and sub.refusal_reason is not None:
            add(
                "refused",
                "warning",
                f"{name} has no usable price",
                f"Its latest {sub.refusal_broker} quote was refused: {sub.refusal_reason}. "
                "Nothing trades on it until a current quote arrives.",
            )
        elif (
            market_open
            and sub.is_active
            and sub.state == "active"
            and sub.last_tick_age_seconds is not None
            and sub.last_tick_age_seconds > stale_after_seconds
        ):
            add(
                "stale",
                "warning",
                f"{name} has a stale price",
                f"The last price arrived {format_age(sub.last_tick_age_seconds)} while the market "
                "is open.",
            )
    return sorted(items, key=lambda item: (SEVERITY_ORDER[item.severity], item.title))
