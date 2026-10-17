"""What the paper desk shows *before* an operator acts: what adding a subscription would do, and
what closing a position would do. Both are read-only -- nothing is created, filled or written --
and both answer with the same rules the real action uses (the same helpers, not copies of them),
so a preview that says "fits" or "fills at" is a statement about what the engine would do, not a
second opinion.

An estimate is labelled as one: a quote moves between the preview and the action, a margin is this
system's approximation (never SPAN), and the risk gate (position limits, circuit band) judges the
closing order itself at the moment it is placed.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.brokers.futures_contracts import NoFuturesContractError
from src.core.config import get_settings
from src.data.broker_provider import NoRealDataSourceError
from src.engine.paper_trading.depth import DepthUnavailableError, load_depth
from src.engine.paper_trading.margin import (
    approximate_futures_margin,
    check_futures_margin,
    check_max_loss_margin,
)
from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.paper_trading.option_strategies import max_loss, pick_expiry, plan_entry
from src.engine.paper_trading.order_book import OrderBookProvider, Side, book_for, simulate_fill
from src.engine.paper_trading.tick_feed import get_latest_tick
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.kill_switch import get_kill_switch_state
from src.orchestration.paper_subscription_controls import (
    PriceUnavailableError,
    SubscriptionStateError,
    _load,
    _price_for_close,
    assert_no_active_duplicate,
    open_positions,
)
from src.orchestration.paper_trading import equity_entry_quantity, fill_costs


async def symbol_listing(provider: Any, symbol: str) -> bool | None:
    """True/False: a connected broker does/does not list the symbol. None: cannot tell (no broker
    is connected, or the provider is the generated-data one) -- the subscription is then allowed
    and its state says why it is not running."""
    check = getattr(provider, "symbol_listed", None)
    if check is None:
        return None
    try:
        return bool(await check(symbol))
    except NoRealDataSourceError:
        return None


# ---- enrolment ----


@dataclass
class OptionLegPreview:
    symbol: str
    side: str  # "buy" | "sell"
    option_type: str  # "CE" | "PE"
    strike: float
    quantity: int
    price: float | None


@dataclass
class OptionScenario:
    """The structure a signal in one direction would open."""

    signal: str  # "BUY" | "SELL"
    view: str  # "bullish" | "bearish"
    legs: list[OptionLegPreview]
    max_loss: float | None  # None: a leg's price is not known
    fits: bool | None
    note: str | None = None


@dataclass
class EnrolPreview:
    symbol: str
    instrument_kind: str
    listing: str  # "listed" | "not_listed" | "unknown"
    price: float | None = None
    price_symbol: str | None = None
    equity: dict[str, Any] | None = None
    future: dict[str, Any] | None = None
    option: dict[str, Any] | None = None
    scenarios: list[OptionScenario] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # enrolling would be refused
    warnings: list[str] = field(default_factory=list)  # enrolling works, but...


async def preview_enrolment(
    db: AsyncSession,
    provider: Any,
    *,
    symbol: str,
    instrument_kind: str,
    initial_capital: float,
    position_size_pct: float,
    lots: int,
    option_strategy: str,
    spread_width: int,
    strategy_version_id: uuid.UUID | None = None,
    today: date | None = None,
) -> EnrolPreview:
    """What adding this subscription would do, without adding it. `problems` are the reasons the
    real enrolment would refuse; `warnings` are what it would allow but the operator should know."""
    settings = get_settings()
    today = today or datetime.now(IST).date()
    symbol = symbol.strip().upper()
    out = EnrolPreview(symbol=symbol, instrument_kind=instrument_kind, listing="unknown")

    listed = await symbol_listing(provider, symbol)
    if listed is True:
        out.listing = "listed"
    elif listed is False:
        out.listing = "not_listed"
        out.problems.append(
            f"no connected broker lists an NSE equity or index named {symbol!r}: paper trading "
            "runs on real symbols"
        )
        return out
    else:
        out.warnings.append(
            "No broker is connected, so this symbol can't be checked and nothing can be priced. "
            "It can still be added; it will wait until a real feed exists."
        )

    if strategy_version_id is not None:
        try:
            await assert_no_active_duplicate(
                db,
                strategy_version_id=strategy_version_id,
                symbol=symbol,
                instrument_kind=instrument_kind,
            )
        except SubscriptionStateError as exc:
            out.problems.append(str(exc))

    if instrument_kind == "equity":
        await _preview_equity(out, provider, initial_capital, position_size_pct)
    elif instrument_kind == "future":
        await _preview_future(out, provider, settings, today, lots, initial_capital)
    else:
        await _preview_option(
            out,
            provider,
            settings,
            today,
            lots=lots,
            strategy=option_strategy,
            width=spread_width,
            capital=initial_capital,
        )
    return out


async def _indicative(provider: Any, symbol: str) -> float | None:
    lookup = getattr(provider, "indicative_price", None)
    if lookup is None:
        return None
    try:
        return await lookup(symbol)  # type: ignore[no-any-return]
    except NoRealDataSourceError:
        return None


async def _preview_equity(
    out: EnrolPreview, provider: Any, capital: float, size_pct: float
) -> None:
    price = await _indicative(provider, out.symbol)
    out.price, out.price_symbol = price, out.symbol
    entry_capital = capital * size_pct / 100.0
    if price is None:
        out.warnings.append("No price is available to size an entry against.")
        out.equity = {"entry_capital": entry_capital, "shares": None, "notional": None}
        return
    shares = equity_entry_quantity(capital, size_pct, price)
    out.equity = {
        "entry_capital": entry_capital,
        "shares": shares,
        "notional": shares * price,
    }
    if shares == 0:
        out.warnings.append(
            f"One share costs Rs {price:,.2f}, more than the Rs {entry_capital:,.0f} "
            f"({size_pct:g}% of capital) an entry may use, so it would never buy. "
            "Raise the capital or the size."
        )


async def _preview_future(
    out: EnrolPreview, provider: Any, settings: Any, today: date, lots: int, capital: float
) -> None:
    resolver = getattr(provider, "futures_contract", None)
    if resolver is None:
        out.problems.append(
            "futures need a connected broker: its instrument master is the only source of the "
            "contract and its lot size"
        )
        return
    try:
        contract = await resolver(
            out.symbol, today=today, roll_days=settings.paper_futures_roll_days
        )
    except NoRealDataSourceError as exc:
        out.problems.append(f"futures need a connected broker ({exc})")
        return
    except NoFuturesContractError as exc:
        out.problems.append(str(exc))
        return
    quantity = lots * contract.lot_size
    price = await _indicative(provider, contract.symbol)
    out.price, out.price_symbol = price, contract.symbol
    future: dict[str, Any] = {
        "contract": contract.symbol,
        "expiry": contract.expiry.isoformat(),
        "rolls_on": (
            contract.expiry - timedelta(days=settings.paper_futures_roll_days)
        ).isoformat(),
        "lot_size": contract.lot_size,
        "lots": lots,
        "quantity": quantity,
        "notional": None,
        "margin": None,
        "margin_limit": capital * settings.paper_futures_max_margin_utilisation_pct / 100.0,
        "margin_pct": settings.paper_futures_margin_pct,
        "fits": None,
    }
    out.future = future
    if not price:
        out.warnings.append("No price is available, so the margin can't be estimated.")
        return
    future["notional"] = price * quantity
    future["margin"] = approximate_futures_margin(
        price=price, quantity=quantity, margin_pct=settings.paper_futures_margin_pct
    ).approximate_margin
    check = check_futures_margin(
        price=price,
        quantity=quantity,
        capital=capital,
        margin_pct=settings.paper_futures_margin_pct,
        max_utilisation_pct=settings.paper_futures_max_margin_utilisation_pct,
    )
    future["fits"] = check.ok
    if not check.ok:
        out.problems.append(
            f"{lots} lot(s) of {contract.symbol} ({contract.lot_size} each) do not fit this "
            f"account: {check.reason}. Raise initial_capital or trade fewer lots."
        )


async def _preview_option(
    out: EnrolPreview,
    provider: Any,
    settings: Any,
    today: date,
    *,
    lots: int,
    strategy: str,
    width: int,
    capital: float,
) -> None:
    lister = getattr(provider, "option_contracts", None)
    if lister is None:
        out.problems.append(
            "options need a connected broker: its instrument master is the only source of the "
            "contracts and their lot sizes"
        )
        return
    try:
        contracts = await lister(out.symbol)
    except NoRealDataSourceError as exc:
        out.problems.append(f"options need a connected broker ({exc})")
        return
    min_days = settings.paper_options_min_days_to_expiry
    expiry = pick_expiry(contracts, today=today, min_days=min_days)
    if expiry is None:
        out.problems.append(
            f"no listed {out.symbol} option expires more than {min_days} days from now"
            f"{' (the broker lists none)' if not contracts else ''}"
        )
        return
    spot = await _indicative(provider, out.symbol)
    out.price, out.price_symbol = spot, out.symbol
    lot_size = next((c.lot_size for c in contracts if c.expiry == expiry), None)
    out.option = {
        "expiry": expiry.isoformat(),
        "lot_size": lot_size,
        "lots": lots,
        "strategy": strategy,
        "spread_width": width,
        "limit": capital * settings.paper_futures_max_margin_utilisation_pct / 100.0,
    }
    if spot is None:
        out.warnings.append("No price for the underlying, so the legs can't be chosen yet.")
        return
    for signal in ("BUY", "SELL"):
        plan = plan_entry(
            strategy,
            signal,
            spot=spot,
            contracts=contracts,
            today=today,
            min_days=min_days,
            lots=lots,
            width=width,
        )
        if plan is None:
            out.scenarios.append(
                OptionScenario(
                    signal=signal,
                    view="bullish" if signal == "BUY" else "bearish",
                    legs=[],
                    max_loss=None,
                    fits=None,
                    note="No suitable contract is listed for this direction.",
                )
            )
            continue
        legs = [
            OptionLegPreview(
                symbol=leg.contract.symbol,
                side=leg.side,
                option_type=leg.contract.option_type,
                strike=leg.contract.strike,
                quantity=leg.quantity,
                price=await _indicative(provider, leg.contract.symbol),
            )
            for leg in plan.legs
        ]
        scenario = OptionScenario(
            signal=signal, view=plan.direction, legs=legs, max_loss=None, fits=None
        )
        if all(leg.price is not None for leg in legs):
            net_debit = sum(
                (1 if leg.side == "buy" else -1) * leg.quantity * (leg.price or 0.0) for leg in legs
            )
            worst = max_loss(plan.legs, net_debit=net_debit)
            scenario.max_loss = worst
            check = check_max_loss_margin(
                max_loss=worst,
                capital=capital,
                max_utilisation_pct=settings.paper_futures_max_margin_utilisation_pct,
            )
            scenario.fits = check.ok
            if not check.ok:
                scenario.note = check.reason
        else:
            scenario.note = "A leg has no price yet, so the largest loss can't be worked out."
        out.scenarios.append(scenario)
    if out.scenarios and all(s.legs and s.fits is False for s in out.scenarios):
        out.warnings.append(
            "Neither direction fits this account, so it would not enter. "
            "Raise the capital or trade fewer lots."
        )


# ---- closing ----


@dataclass
class CloseLegPreview:
    symbol: str
    side: str  # the order that closes it: "sell" | "buy"
    quantity: int
    avg_cost: float
    reference_price: float | None = None
    reference_age_seconds: float | None = None
    reference_source: str | None = None
    book_age_seconds: float | None = None
    # What the book is: `real` (the broker's live depth) or `synthetic` (generated, in a demo or
    # test setup). An estimate resting on a generated book must say so.
    book_source: str | None = None
    est_avg_price: float | None = None
    est_filled: int = 0
    est_remaining: int = 0
    est_costs: float | None = None
    est_gross_pnl: float | None = None
    problem: str | None = None


@dataclass
class ClosePreview:
    subscription_id: str
    symbol: str
    market_open: bool
    legs: list[CloseLegPreview]
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    est_costs_total: float | None = None
    est_gross_pnl: float | None = None
    can_close: bool = False


async def preview_close(
    db: AsyncSession,
    redis: Redis,
    subscription_id: uuid.UUID,
    *,
    order_book_provider: OrderBookProvider,
    now: datetime | None = None,
    price: float | None = None,
) -> ClosePreview:
    """What closing the subscription's position would do right now: each open leg (sold first, the
    order a close takes), the price it would use, the book it would walk, the fill that walk gives,
    and what that fill would cost. The same price rule, book and fill simulation as
    `close_position`; nothing is written. `price` is the price the operator would supply: it
    stands in for a missing fresh tick (as in the close itself) and, like there, means one price
    for one position, so it is ignored for a subscription holding several legs."""
    now = now or datetime.now(UTC)
    subscription = await _load(db, subscription_id)
    held = await open_positions(db, subscription)
    out = ClosePreview(
        subscription_id=str(subscription.id),
        symbol=subscription.symbol,
        market_open=is_market_open_ist(now),
        legs=[],
    )
    if not held:
        out.blockers.append(f"{subscription.symbol} has no open position to close")
        return out
    kill = await get_kill_switch_state(db, "paper")
    if kill.tripped:
        out.blockers.append(f"the paper kill switch is tripped: {kill.trip_reason}")

    supplied = price if len(held) == 1 else None
    if price is not None and supplied is None:
        out.notes.append("A price you supply applies to a one-position subscription only.")
    stopped_by: str | None = None
    for position in held:
        side: Side = "sell" if position.quantity > 0 else "buy"
        leg = CloseLegPreview(
            symbol=position.symbol,
            side=side,
            quantity=abs(position.quantity),
            avg_cost=position.avg_cost,
        )
        out.legs.append(leg)
        if stopped_by is not None:
            leg.problem = f"would be left open: {stopped_by}"
            continue
        await _estimate_leg(redis, order_book_provider, subscription, leg, now, supplied)
        if position.quantity < 0 and (leg.problem or leg.est_remaining > 0):
            # Closing stops at a sold leg it cannot fully buy back: its hedges stay open.
            stopped_by = (
                f"the sold {position.symbol} leg can't be fully closed, and its hedges stay"
            )
    priced = [leg for leg in out.legs if leg.est_costs is not None]
    if priced:
        out.est_costs_total = sum(leg.est_costs or 0.0 for leg in priced)
        out.est_gross_pnl = sum(leg.est_gross_pnl or 0.0 for leg in priced)
    out.can_close = not out.blockers and any(leg.est_filled > 0 for leg in out.legs)
    out.notes.append(
        "An estimate from the latest price and order book. The risk gate also judges the closing "
        "order (position limits, circuit band) when it is placed."
    )
    if not out.market_open:
        out.notes.append("The market is shut: there is usually no fresh price or book to close on.")
    return out


async def _estimate_leg(
    redis: Redis,
    order_book_provider: OrderBookProvider,
    subscription: PaperTradingSubscription,
    leg: CloseLegPreview,
    now: datetime,
    supplied: float | None = None,
) -> None:
    try:
        price, _basis, source = await _price_for_close(redis, leg.symbol, supplied, now)
    except PriceUnavailableError as exc:
        leg.problem = str(exc)
        return
    leg.reference_price = price
    leg.reference_source = source
    tick = await get_latest_tick(redis, leg.symbol) if supplied is None else None
    if tick is not None:
        leg.reference_age_seconds = max(0.0, now.timestamp() - tick.timestamp_ms / 1000.0)
    try:
        book = await book_for(order_book_provider, leg.symbol, side=leg.side, mid_price=price)  # type: ignore[arg-type]
    except DepthUnavailableError as exc:
        leg.problem = f"no live order book to fill the closing order: {exc}"
        return
    leg.book_source = getattr(order_book_provider, "data_source", None)
    depth = await load_depth(redis, leg.symbol)
    if depth is not None:
        leg.book_age_seconds = max(0.0, now.timestamp() - depth.timestamp_ms / 1000.0)
    fill = simulate_fill(side=leg.side, quantity=leg.quantity, book=book)  # type: ignore[arg-type]
    leg.est_filled = fill.filled_quantity
    leg.est_remaining = fill.remaining_quantity
    leg.est_avg_price = fill.avg_fill_price
    if fill.filled_quantity == 0 or fill.avg_fill_price is None:
        leg.problem = "there is no depth to fill the closing order against"
        return
    costs, _breakdown = fill_costs(leg.side, fill, instrument_kind=subscription.instrument_kind)  # type: ignore[arg-type]
    leg.est_costs = costs
    # A long closes by selling above its cost to gain; a short closes by buying below it.
    direction = 1 if leg.side == "sell" else -1
    leg.est_gross_pnl = direction * (fill.avg_fill_price - leg.avg_cost) * fill.filled_quantity
