"""The server's twin of the Paper Desk's attention rules (src.orchestration.paper_attention).

The shared cases in e2e/fixtures/paper-attention.json are run by the console's own implementation
too (e2e/paper-trading-logic.spec.ts), so the two cannot drift: the alerts that message a person
must say what the screen says.
"""

import json
import uuid
from pathlib import Path

import pytest

from src.core.config import Settings
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_attention import (
    AttentionSubject,
    attention_items,
    format_age,
    subject_from_view,
    subscription_name,
)
from src.orchestration.paper_subscription_controls import SubscriptionView

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "e2e" / "fixtures" / "paper-attention.json").read_text(
        encoding="utf-8"
    )
)


def _subject(raw: dict) -> AttentionSubject:
    tick = raw["last_tick"]
    return AttentionSubject(
        id=raw["id"],
        symbol=raw["symbol"],
        instrument_kind=raw["instrument_kind"],
        option_strategy=raw["option_strategy"],
        is_active=raw["is_active"],
        managed=raw["managed"],
        state=raw["state"],
        state_reason=raw["state_reason"],
        open_legs=raw["open_legs"],
        unrealized_pnl=raw["unrealized_pnl"],
        last_tick_age_seconds=tick["age_seconds"] if tick else None,
        refusal_broker=(raw.get("quote_refusal") or {}).get("broker"),
        refusal_reason=(raw.get("quote_refusal") or {}).get("reason"),
    )


def test_the_stale_limit_is_the_one_the_console_mirrors():
    assert (
        Settings.model_fields["paper_stale_price_seconds"].default == FIXTURE["stale_price_seconds"]
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_the_server_says_what_the_console_says(case):
    items = attention_items(
        [_subject(raw) for raw in case["subscriptions"]],
        market_open=case["market_open"],
        stale_after_seconds=FIXTURE["stale_price_seconds"],
    )

    assert [
        {
            "id": item.id,
            "severity": item.severity,
            "title": item.title,
            "detail": item.detail,
            "action": item.action,
        }
        for item in items
    ] == case["expected"]


def test_an_item_carries_its_kind_and_subscription():
    case = next(
        c for c in FIXTURE["cases"] if "switched-off subscription still holding" in c["name"]
    )

    (item,) = attention_items(
        [_subject(raw) for raw in case["subscriptions"]], market_open=False, stale_after_seconds=120
    )

    assert (item.kind, item.subscription_id) == ("unmanaged", "a1")


def test_the_stale_limit_is_a_parameter_not_a_constant():
    subject = _subject(
        {**FIXTURE["cases"][0]["subscriptions"][0], "last_tick": {"age_seconds": 60}}
    )

    assert attention_items([subject], market_open=True, stale_after_seconds=120) == []
    assert [
        i.kind for i in attention_items([subject], market_open=True, stale_after_seconds=30)
    ] == ["stale"]


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (None, "never"),
        (0.4, "just now"),
        (1, "1s ago"),
        (59.5, "60s ago"),  # a half rounds up, as it does in the browser
        (60, "1m ago"),
        (150, "3m ago"),
        (3599, "60m ago"),
        (3600, "1.0h ago"),
        (4500, "1.3h ago"),  # 1.25 exactly: up, where Python's own rounding would say 1.2
        (86399, "24.0h ago"),
        (86400, "1d ago"),
        (129600, "2d ago"),  # 1.5 days
    ],
)
def test_how_long_ago_is_said_the_way_the_console_says_it(seconds, said):
    assert format_age(seconds) == said


def test_a_subscription_is_named_for_what_it_is():
    def name(kind, strategy=None):
        return subscription_name(
            AttentionSubject(
                "x", "NIFTY", kind, strategy, True, True, "active", None, 0, None, None
            )
        )

    assert name("equity") == "NIFTY"
    assert name("future") == "NIFTY future"
    assert name("option", "vertical_spread") == "NIFTY credit spread"
    assert name("option", "long_option") == "NIFTY long option"
    assert name("option") == "NIFTY"  # an option with no strategy recorded is just its symbol
    assert name("option", "iron_condor") == "NIFTY iron_condor"


def test_a_view_becomes_a_subject_with_the_age_only_when_a_price_exists():
    subscription = PaperTradingSubscription(
        id=uuid.uuid4(),
        symbol="TCS",
        instrument_kind="equity",
        option_strategy=None,
        is_active=False,
    )
    view = SubscriptionView(
        subscription=subscription,
        state="inactive",
        state_reason=None,
        quantity=10,
        avg_cost=100.0,
        realized_pnl=0.0,
        costs_total=0.0,
        open_legs=1,
        last_tick=None,
        last_tick_age_seconds=None,
        unrealized_pnl=None,
    )

    subject = subject_from_view(view)

    assert subject.id == str(subscription.id)
    assert subject.managed is False  # switched off and still holding a position
    assert subject.last_tick_age_seconds is None
    assert subject.open_legs == 1


def test_a_view_with_a_price_hands_on_how_old_it_is():
    from src.engine.paper_trading.tick_feed import Tick

    subscription = PaperTradingSubscription(
        id=uuid.uuid4(),
        symbol="TCS",
        instrument_kind="equity",
        option_strategy=None,
        is_active=True,
    )
    view = SubscriptionView(
        subscription=subscription,
        state="active",
        state_reason=None,
        quantity=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        costs_total=0.0,
        last_tick=Tick("TCS", 100.0, 0, "real"),
        last_tick_age_seconds=42.5,
    )

    assert subject_from_view(view).last_tick_age_seconds == 42.5


def test_subjects_and_items_are_immutable_values():
    from dataclasses import FrozenInstanceError

    from src.orchestration.paper_attention import AttentionItem

    subject = AttentionSubject(
        "x", "TCS", "equity", None, True, True, "active", None, 0, None, None
    )
    item = AttentionItem("x:stale", "stale", "warning", "t", "d", "x", None)

    for value, attribute in ((subject, "symbol"), (item, "title")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, "other")
        assert not hasattr(value, "__dict__")  # slotted, so no stray attributes either


def test_a_subject_with_no_refusal_says_none_not_an_empty_string():
    subject = AttentionSubject(
        "x", "TCS", "equity", None, True, True, "active", None, 0, None, None
    )

    assert subject.refusal_broker is None
    assert subject.refusal_reason is None


def test_a_view_with_a_refused_quote_hands_on_who_refused_it_and_why():
    from datetime import UTC, datetime

    from src.engine.paper_trading.tick_feed import QuoteRefusal

    subscription = PaperTradingSubscription(
        id=uuid.uuid4(),
        symbol="TCS",
        instrument_kind="equity",
        option_strategy=None,
        is_active=True,
    )
    view = SubscriptionView(
        subscription=subscription,
        state="active",
        state_reason=None,
        quantity=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        costs_total=0.0,
        quote_refusal=QuoteRefusal(
            broker="upstox", reason="too old", exchange_time=None, at=datetime.now(UTC)
        ),
    )
    plain = SubscriptionView(
        subscription=subscription,
        state="active",
        state_reason=None,
        quantity=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        costs_total=0.0,
    )

    subject = subject_from_view(view)
    assert (subject.refusal_broker, subject.refusal_reason) == ("upstox", "too old")
    assert (subject_from_view(plain).refusal_broker, subject_from_view(plain).refusal_reason) == (
        None,
        None,
    )
