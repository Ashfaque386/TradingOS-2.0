"""Paper (and live) trading must never act on generated data for a real symbol.

Before this, a subscription on a real symbol got its daily signals from `FakeDailyPriceProvider`
whenever the data lake held fewer than five bars for it -- live trading too, through the same
provider -- while its ticks came from the market: trades driven by fiction. These tests pin the
replacement behaviour: a subscription without real history is skipped, loudly, and nothing is
invented. They also cover the "unmanaged position" flag: an open position on an inactive
subscription has no stop-loss watching it, and the dashboards now say so.
"""

import uuid
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data import lake
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider, InsufficientHistoryError
from src.main import app
from src.models.instrument import Instrument
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy import Strategy, StrategyStatus
from src.orchestration.live_trading import run_live_daily_signal_generation
from src.orchestration.paper_trading import enroll_in_paper_trading, run_daily_signal_generation
from src.orchestration.screener import ScreenerFilters, screen_instruments
from src.orchestration.strategies import create_strategy, create_version_with_validation

AS_OF = pd.Timestamp("2026-09-09")
_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


def _bars(n: int, start: str) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    closes = [100.0 + i for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * n},
        index=idx,
    )


async def _paper_subscription(db_session_factory, symbol: str) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"Guard-{symbol}", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol=symbol,
            builtin_strategy="sma_crossover",
            sma_window=15,
        )
        return subscription.id


# ---- paper signal generation ----


async def test_a_paper_subscription_without_real_history_is_skipped_not_signalled(
    db_session_factory, tmp_path: Path, capsys
):
    await _paper_subscription(db_session_factory, "RELIANCE")
    provider = DataLakePriceProvider(root=tmp_path / "lake")  # empty lake, no fallback

    async with db_session_factory() as db:
        created = await run_daily_signal_generation(db, price_provider=provider, as_of=AS_OF)

    assert created == []  # no signal, no crash, and above all no generated bars
    out = capsys.readouterr().out
    assert "paper_trading.signal_skipped_awaiting_history" in out
    assert "RELIANCE" in out


async def test_one_subscription_awaiting_history_does_not_block_the_others(
    db_session_factory, tmp_path: Path
):
    """The skip is per subscription: the loop carries on to the next one (it must not abort)."""
    await _paper_subscription(db_session_factory, "NOHISTORY")
    await _paper_subscription(db_session_factory, "HASHISTORY")
    lake.write_daily_bars(tmp_path / "lake", "HASHISTORY", _bars(40, "2026-07-01"))
    asked: list[str] = []

    class _Recording:
        def __init__(self, inner):
            self._inner = inner

        def daily_bars(self, symbol, *, as_of, lookback_days):
            asked.append(symbol)
            return self._inner.daily_bars(symbol, as_of=as_of, lookback_days=lookback_days)

    provider = _Recording(DataLakePriceProvider(root=tmp_path / "lake"))
    async with db_session_factory() as db:
        await run_daily_signal_generation(db, price_provider=provider, as_of=AS_OF)  # no raise

    assert sorted(asked) == ["HASHISTORY", "NOHISTORY"]  # both were tried; the first did not abort


async def test_with_an_explicit_synthetic_provider_the_cycle_still_runs(db_session_factory):
    """Synthetic mode is the only way back to generated bars, and it is a deliberate choice."""
    await _paper_subscription(db_session_factory, "DEMOSTOCK")
    provider = FakeDailyPriceProvider(seed_by_symbol={"DEMOSTOCK": 1})

    async with db_session_factory() as db:
        created = await run_daily_signal_generation(
            db, price_provider=provider, as_of=pd.Timestamp("2026-09-09")
        )

    assert len(created) == 1 and created[0].signal_type == "BUY"


# ---- live signal generation: real money, same rule ----


async def test_a_live_subscription_without_real_history_gets_no_signal(
    db_session_factory, tmp_path: Path, capsys
):
    async with db_session_factory() as db:
        strategy = Strategy(
            name="LiveGuard", objective="o", status=StrategyStatus.LIVE_ELIGIBLE.value
        )
        db.add(strategy)
        await db.flush()
        db.add(
            LiveTradingSubscription(strategy_id=strategy.id, symbol="INFY", broker_name="zerodha")
        )
        await db.commit()

    provider = DataLakePriceProvider(root=tmp_path / "lake")
    async with db_session_factory() as db:
        updated = await run_live_daily_signal_generation(db, price_provider=provider, as_of=AS_OF)

    assert updated == []
    async with db_session_factory() as db:
        sub = (await db.execute(select(LiveTradingSubscription))).scalar_one()
        assert sub.last_signal_type is None  # nothing was written from invented bars
    assert "live_trading.signal_skipped_awaiting_history" in capsys.readouterr().out


# ---- screener ----


async def test_the_screener_reports_a_symbol_without_history_instead_of_screening_fiction(
    db_session_factory, tmp_path: Path
):
    async with db_session_factory() as db:
        db.add(
            Instrument(
                symbol="TCS",
                exchange="NSE",
                instrument_type="equity",
                lot_size=1,
                tick_size=0.05,
                is_active=True,
            )
        )
        await db.commit()
    async with db_session_factory() as db:
        results = await screen_instruments(
            db,
            DataLakePriceProvider(root=tmp_path / "lake"),
            as_of=AS_OF,
            filters=ScreenerFilters(),
        )

    assert len(results) == 1 and results[0].passed is False
    assert results[0].reasons == ("not enough real price history ingested yet",)


# ---- the manual endpoints use the same real provider ----


async def test_the_manual_daily_signal_endpoints_default_to_the_real_lake(
    client, make_user, tmp_path, monkeypatch
):
    """No dependency override here on purpose: an empty lake means no signals from either manual
    endpoint (they used to read a module-level FakeDailyPriceProvider)."""
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path / "lake"))
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    from src.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert app.dependency_overrides.get(get_signal_price_provider) is None
        await make_user("guard-admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "guard-admin@example.com", "password": "supersecret1"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = await client.post(
            "/api/v1/paper-trading/daily-signal-run", json={"as_of": "2026-09-09"}, headers=headers
        )
        assert resp.status_code == 201 and resp.json() == []
        resp = await client.post(
            "/api/v1/live-trading/daily-signal-run", json={"as_of": "2026-09-09"}, headers=headers
        )
        assert resp.status_code == 200 and resp.json() == []
    finally:
        get_settings.cache_clear()


# ---- unmanaged positions ----


@pytest.fixture
def _synthetic_prices_and_redis(redis_client):
    app.dependency_overrides[get_signal_price_provider] = lambda: FakeDailyPriceProvider()
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_signal_price_provider, None)
    app.dependency_overrides.pop(get_redis, None)


async def _open_a_paper_position(client, headers) -> str:
    create = await client.post(
        "/api/v1/strategies", json={"name": "UnmanagedDemo", "objective": "o"}, headers=headers
    )
    version_id = create.json()["versions"][0]["id"]
    enroll = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={"strategy_version_id": version_id, "symbol": "DEMOSTOCK"},
        headers=headers,
    )
    subscription_id = enroll.json()["id"]
    signals = await client.post(
        "/api/v1/paper-trading/daily-signal-run", json={"as_of": "2026-09-09"}, headers=headers
    )
    reference = next(s for s in signals.json() if s["subscription_id"] == subscription_id)[
        "reference_price"
    ]
    fill = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference},
        headers=headers,
    )
    assert fill.json()["side"] == "buy"
    return subscription_id


async def test_an_open_position_is_flagged_unmanaged_once_its_subscription_is_inactive(
    client, make_user, db_session_factory, _synthetic_prices_and_redis
):
    await make_user("unmanaged@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "unmanaged@example.com", "password": "supersecret1"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    subscription_id = await _open_a_paper_position(client, headers)

    # While the subscription is active the position is managed everywhere.
    unrealized = (await client.get("/api/v1/paper-trading/pnl/unrealized", headers=headers)).json()
    summary = (await client.get("/api/v1/portfolio/summary", headers=headers)).json()
    positions = (await client.get("/api/v1/orders/positions", headers=headers)).json()
    assert unrealized["unmanaged_positions"] == 0
    assert (
        summary["paper_open_position_count"] == 1 and summary["paper_unmanaged_position_count"] == 0
    )
    assert [p["managed"] for p in positions if p["mode"] == "paper"] == [True]

    async with db_session_factory() as db:
        sub = await db.get(PaperTradingSubscription, uuid.UUID(subscription_id))
        sub.is_active = False
        await db.commit()

    unrealized = (await client.get("/api/v1/paper-trading/pnl/unrealized", headers=headers)).json()
    summary = (await client.get("/api/v1/portfolio/summary", headers=headers)).json()
    positions = (await client.get("/api/v1/orders/positions", headers=headers)).json()
    assert unrealized["unmanaged_positions"] == 1
    assert summary["paper_open_position_count"] == 1  # still open, still counted...
    assert summary["paper_unmanaged_position_count"] == 1  # ...and now flagged
    assert [p["managed"] for p in positions if p["mode"] == "paper"] == [False]


async def test_the_unrealized_endpoint_labels_a_missing_feed_none_not_synthetic(
    client, make_user, _synthetic_prices_and_redis, monkeypatch
):
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    from src.core.config import get_settings

    get_settings.cache_clear()
    try:
        await make_user("nofeed@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        login = await client.post(
            "/api/v1/auth/login", json={"email": "nofeed@example.com", "password": "supersecret1"}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        off = (await client.get("/api/v1/paper-trading/pnl/unrealized", headers=headers)).json()
        monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")
        get_settings.cache_clear()
        on = (await client.get("/api/v1/paper-trading/pnl/unrealized", headers=headers)).json()
    finally:
        get_settings.cache_clear()

    assert off["price_source"] == "none"
    assert on["price_source"] == "synthetic"


def test_insufficient_history_is_an_exception_callers_can_catch_by_type():
    with pytest.raises(InsufficientHistoryError):
        raise InsufficientHistoryError("X", have=0, need=5)
