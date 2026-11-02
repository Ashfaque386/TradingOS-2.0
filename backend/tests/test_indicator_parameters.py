"""The indicator parameters: bounded, ruled, and carried back on the series they produced."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from httpx import AsyncClient

from src.api.routes.market_data import get_data_lake_root
from src.core.roles import Role
from src.data import lake
from src.data.lake_status import trading_days_behind
from src.data.nse_calendar import previous_nse_trading_day
from src.engine.paper_trading.market_hours import IST
from src.main import app
from src.models.dataset_freshness_record import DatasetFreshnessRecord

NO_EXTRA_HOLIDAYS: frozenset[date] = frozenset()


@pytest.fixture(autouse=True)
def _lake_in_tmp(tmp_path):
    app.dependency_overrides[get_data_lake_root] = lambda: tmp_path / "lake"
    yield
    app.dependency_overrides.pop(get_data_lake_root, None)


async def _headers(client: AsyncClient, make_user, email: str = "ip@example.com") -> dict[str, str]:
    await make_user(email, "supersecret1", Role.READ_ONLY_AUDITOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _ramp(n: int, start: str) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.0 + i for i in range(n)],
            "volume": [1000] * n,
        },
        index=idx,
    )


@pytest.mark.parametrize(
    "query",
    [
        "lookback_days=4",
        "lookback_days=2001",
        "sma_window=1",
        "sma_window=0",
        "sma_window=401",
        "ema_span=1",
        "ema_span=401",
        "rsi_period=1",
        "rsi_period=101",
        "macd_fast=1",
        "macd_fast=101",
        "macd_slow=2",
        "macd_slow=201",
        "macd_signal_period=1",
        "macd_signal_period=101",
        "bollinger_window=1",
        "bollinger_window=401",
        "bollinger_std=0",
        "bollinger_std=-1",
        "bollinger_std=5.01",
    ],
)
async def test_a_parameter_outside_its_bounds_is_a_422_not_a_server_error(
    client: AsyncClient, make_user, query: str
):
    headers = await _headers(client, make_user)
    resp = await client.get(f"/api/v1/market-data/indicators/ANY?{query}", headers=headers)
    assert resp.status_code == 422, query


@pytest.mark.parametrize(
    "query",
    [
        "lookback_days=5",
        "lookback_days=2000",
        "sma_window=2",
        "sma_window=400",
        "ema_span=2",
        "rsi_period=2",
        "rsi_period=100",
        "macd_fast=2&macd_slow=3",
        "macd_fast=99&macd_slow=200",
        "macd_signal_period=2",
        "bollinger_window=2",
        "bollinger_std=0.1",
        "bollinger_std=5",
    ],
)
async def test_each_bound_itself_is_accepted(client: AsyncClient, make_user, query: str):
    headers = await _headers(client, make_user)
    resp = await client.get(f"/api/v1/market-data/indicators/ANY?{query}", headers=headers)
    assert resp.status_code == 200, query


async def test_a_fast_average_that_is_not_shorter_than_the_slow_one_is_refused_with_both_numbers(
    client: AsyncClient, make_user
):
    headers = await _headers(client, make_user)
    equal = await client.get(
        "/api/v1/market-data/indicators/ANY?macd_fast=26&macd_slow=26", headers=headers
    )
    longer = await client.get(
        "/api/v1/market-data/indicators/ANY?macd_fast=30&macd_slow=26", headers=headers
    )
    shorter = await client.get(
        "/api/v1/market-data/indicators/ANY?macd_fast=25&macd_slow=26", headers=headers
    )

    assert equal.status_code == 422 and longer.status_code == 422
    assert equal.json()["detail"] == (
        "The MACD's fast average (26) must be shorter than its slow one (26)."
    )
    assert longer.json()["detail"] == (
        "The MACD's fast average (30) must be shorter than its slow one (26)."
    )
    assert shorter.status_code == 200


async def test_the_series_carries_the_parameters_it_was_computed_with_the_defaults_when_none_given(
    client: AsyncClient, make_user, tmp_path
):
    start = (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
    lake.write_daily_bars(tmp_path / "lake", "ECHO", _ramp(40, start))
    headers = await _headers(client, make_user)

    default = (await client.get("/api/v1/market-data/indicators/ECHO", headers=headers)).json()
    chosen = (
        await client.get(
            "/api/v1/market-data/indicators/ECHO?lookback_days=120&sma_window=5&ema_span=6"
            "&rsi_period=7&macd_fast=3&macd_slow=8&macd_signal_period=4"
            "&bollinger_window=9&bollinger_std=1.5",
            headers=headers,
        )
    ).json()

    assert (default["sma_window"], default["ema_span"], default["rsi_period"]) == (20, 20, 14)
    assert (default["macd_fast"], default["macd_slow"], default["macd_signal_period"]) == (
        12,
        26,
        9,
    )
    assert (default["bollinger_window"], default["bollinger_std"]) == (20, 2.0)
    assert default["lookback_days"] == 250
    assert (chosen["sma_window"], chosen["ema_span"], chosen["rsi_period"]) == (5, 6, 7)
    assert (chosen["macd_fast"], chosen["macd_slow"], chosen["macd_signal_period"]) == (3, 8, 4)
    assert (chosen["bollinger_window"], chosen["bollinger_std"]) == (9, 1.5)
    assert chosen["lookback_days"] == 120
    # and they are what was drawn: a 5-bar SMA of the ramp is filled from the fifth bar
    assert chosen["sma"][3] is None and chosen["sma"][4] == pytest.approx(102.0)
    assert chosen["bollinger_middle"][7] is None and chosen["bollinger_middle"][8] is not None


async def test_an_empty_series_still_says_what_would_have_been_computed(
    client: AsyncClient, make_user
):
    headers = await _headers(client, make_user)
    body = (
        await client.get(
            "/api/v1/market-data/indicators/NOTHING?macd_fast=5&macd_slow=10&bollinger_std=3",
            headers=headers,
        )
    ).json()

    assert body["dates"] == []
    assert (body["macd_fast"], body["macd_slow"], body["bollinger_std"]) == (5, 10, 3.0)


async def test_each_indicator_shows_its_first_number_after_the_bars_the_console_says_it_needs(
    client: AsyncClient, make_user, tmp_path
):
    """`lib/indicator-params.ts` `warmUps` tells a person how many bars an indicator needs; this
    holds those counts to what the server computes: the first filled index is one less than the
    bars needed."""
    start = (datetime.now(UTC).date() - timedelta(days=120)).isoformat()
    lake.write_daily_bars(tmp_path / "lake", "WARM", _ramp(60, start))
    headers = await _headers(client, make_user)

    body = (
        await client.get(
            "/api/v1/market-data/indicators/WARM?lookback_days=200&sma_window=5&ema_span=6"
            "&rsi_period=7&macd_fast=3&macd_slow=8&macd_signal_period=4&bollinger_window=9",
            headers=headers,
        )
    ).json()

    def first_filled(series: list[float | None]) -> int:
        return next(i for i, value in enumerate(series) if value is not None)

    assert first_filled(body["sma"]) + 1 == 5  # smaWindow
    assert first_filled(body["ema"]) + 1 == 6  # emaSpan
    assert first_filled(body["rsi"]) + 1 == 7 + 1  # rsiPeriod + 1
    assert first_filled(body["macd"]) + 1 == 8  # macdSlow
    assert first_filled(body["macd_signal"]) + 1 == 8 + 4 - 1  # macdSlow + macdSignal - 1
    assert first_filled(body["bollinger_middle"]) + 1 == 9  # bollingerWindow


async def test_a_window_longer_than_the_history_gives_a_series_of_nulls_not_invented_numbers(
    client: AsyncClient, make_user, tmp_path
):
    start = (datetime.now(UTC).date() - timedelta(days=40)).isoformat()
    lake.write_daily_bars(tmp_path / "lake", "SHORT", _ramp(10, start))
    headers = await _headers(client, make_user)

    body = (
        await client.get("/api/v1/market-data/indicators/SHORT?sma_window=50", headers=headers)
    ).json()

    assert len(body["dates"]) == 10
    assert body["sma"] == [None] * 10


# ---- how far behind the data lake is ----


def test_a_lake_that_has_reached_the_required_day_or_passed_it_is_zero_days_behind():
    required = date(2026, 11, 2)
    assert trading_days_behind(required, required, extra_holidays=NO_EXTRA_HOLIDAYS) == 0
    assert trading_days_behind(date(2026, 11, 3), required, extra_holidays=NO_EXTRA_HOLIDAYS) == 0


def test_each_trading_day_after_the_newest_up_to_the_required_one_counts_and_weekends_do_not():
    # Fri 30 Oct 2026 -> Mon 2 Nov: Saturday and Sunday are not trading days, Monday is one.
    assert (
        trading_days_behind(date(2026, 10, 30), date(2026, 11, 2), extra_holidays=NO_EXTRA_HOLIDAYS)
        == 1
    )
    # Wed 28 Oct -> Mon 2 Nov: Thursday, Friday, Monday.
    assert (
        trading_days_behind(date(2026, 10, 28), date(2026, 11, 2), extra_holidays=NO_EXTRA_HOLIDAYS)
        == 3
    )


def test_a_listed_holiday_is_not_a_day_the_lake_is_behind_by():
    holiday = frozenset({date(2026, 10, 29)})
    assert trading_days_behind(date(2026, 10, 28), date(2026, 10, 30), extra_holidays=holiday) == 1


def test_a_lake_with_no_daily_data_is_not_behind_by_a_number():
    assert trading_days_behind(None, date(2026, 11, 2), extra_holidays=NO_EXTRA_HOLIDAYS) is None


async def _seed_newest_daily(db_session_factory, day: date) -> None:
    async with db_session_factory() as db:
        db.add(
            DatasetFreshnessRecord(
                symbol="LAGSTOCK", data_type="daily_ohlcv", data_date=day, row_count=1
            )
        )
        await db.commit()


async def test_the_status_says_which_day_backtests_require_and_how_far_behind_the_lake_is(
    client: AsyncClient, make_user, db_session_factory
):
    required = previous_nse_trading_day(datetime.now(IST).date())
    headers = await _headers(client, make_user, "lag@example.com")
    empty = (await client.get("/api/v1/market-data/datalake/status", headers=headers)).json()

    await _seed_newest_daily(db_session_factory, previous_nse_trading_day(required))
    one_behind = (await client.get("/api/v1/market-data/datalake/status", headers=headers)).json()
    await _seed_newest_daily(db_session_factory, required)
    current = (await client.get("/api/v1/market-data/datalake/status", headers=headers)).json()

    assert empty["required_daily_data_date"] == required.isoformat()
    assert empty["daily_data_behind_trading_days"] is None
    assert one_behind["daily_data_behind_trading_days"] == 1
    assert current["daily_data_behind_trading_days"] == 0
    assert current["most_recent_daily_data_date"] == required.isoformat()
