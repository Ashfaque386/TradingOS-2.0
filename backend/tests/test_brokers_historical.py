"""Historical candles and symbol lookup on both broker adapters (real-market-data plan, phase 2).

The payload shapes below are the ones both brokers returned for a real account (probed read-only,
2026-10-16): Kite sends `["2026-07-01T00:00:00+0530", o, h, l, c, volume(, oi)]`, Upstox
`["2026-10-15T00:00:00+05:30", o, h, l, c, volume, oi]` newest first -- and Upstox's daily history
stops at yesterday, which is why it composes today's bar from the intraday endpoint.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from src.brokers import zerodha as zerodha_module
from src.brokers.base import (
    BrokerCredentials,
    BrokerRequestError,
    InstrumentNotFoundError,
    parse_candle,
)
from src.brokers.upstox import UpstoxAdapter
from src.brokers.upstox_instruments import UpstoxInstrumentNotFoundError
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.paper_trading.market_hours import IST, session_is_complete

_CREDENTIALS = BrokerCredentials(api_key="k", access_token="t")


# ---- parse_candle ----


def test_a_kite_candle_parses_with_its_offset_and_no_open_interest():
    candle = parse_candle(["2026-07-01T00:00:00+0530", 1006, 1014, 982.4, 985.3, 1234567])

    assert candle.timestamp == datetime(2026, 7, 1, tzinfo=IST)
    assert (candle.open, candle.high, candle.low, candle.close) == (1006.0, 1014.0, 982.4, 985.3)
    assert candle.volume == 1234567 and candle.open_interest is None


def test_an_upstox_candle_parses_with_open_interest():
    candle = parse_candle(
        ["2026-10-15T09:15:00+05:30", 1099.0, 1126.7, 1095.8, 1110.0, 5000, 17009135]
    )

    assert candle.open_interest == 17009135
    assert candle.timestamp.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    "row",
    [
        ["2026-07-01T00:00:00+0530", 1, 2, 3],  # too short
        ["2026-07-01T00:00:00", 1, 2, 3, 4, 5],  # no timezone: a bar must say which day it is
        ["not a date", 1, 2, 3, 4, 5],
    ],
)
def test_a_malformed_candle_is_refused_not_invented(row):
    with pytest.raises(ValueError):
        parse_candle(row)


def test_a_missing_volume_is_zero_not_an_error():
    assert parse_candle(["2026-07-01T00:00:00+0530", 1, 2, 3, 4, None]).volume == 0


# ---- session_is_complete ----


@pytest.mark.parametrize(
    ("when", "complete"),
    [
        (datetime(2026, 10, 16, 15, 29, tzinfo=IST), False),  # a minute before the close
        (datetime(2026, 10, 16, 15, 30, tzinfo=IST), True),  # at the close
        (datetime(2026, 10, 16, 9, 0, tzinfo=IST), False),  # before the open
        (datetime(2026, 10, 17, 16, 0, tzinfo=IST), False),  # a Saturday: no session at all
    ],
)
def test_the_session_is_complete_only_at_or_after_the_close_on_a_trading_day(when, complete):
    assert session_is_complete(when) is complete


# ---- Zerodha ----

NSE_DUMP = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange\n"
    "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
    "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,1,EQ,INDICES,NSE\n"
    "260105,1016,NIFTY BANK,NIFTY BANK,0,,0,0,1,EQ,INDICES,NSE\n"
    "999999,9999,INFYBEES,SOME ETF,0,,0,0.05,1,EQ,NSE,NSE\n"
    "111111,1111,INFY-BE,INFOSYS BE,0,,0,0.05,1,BE,NSE,NSE\n"  # another series: not the EQ row
    "222222,2222,ONLYBE,ONLY TRADES IN BE,0,,0,0.05,1,BE,NSE,NSE\n"  # no cash-equity row
    "333333,3333,DUPSYM,FIRST LISTING,0,,0,0.05,1,EQ,NSE,NSE\n"
    "444444,4444,DUPSYM,SECOND LISTING,0,,0,0.05,1,EQ,NSE,NSE\n"  # a repeated tradingsymbol
)


@pytest.fixture(autouse=True)
def _fresh_zerodha_token_cache():
    zerodha_module._nse_token_cache = None
    yield
    zerodha_module._nse_token_cache = None


class _Kite:
    """A recording stand-in for api.kite.trade."""

    def __init__(self, candles_by_token=None):
        self.requests: list[httpx.Request] = []
        self.candles_by_token = candles_by_token or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/instruments/NSE":
            return httpx.Response(200, text=NSE_DUMP)
        if request.url.path.startswith("/instruments/historical/"):
            token = request.url.path.split("/")[3]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"candles": self.candles_by_token.get(token, [])},
                },
            )
        return httpx.Response(404, text="unexpected")

    def history_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/instruments/historical/")]


def _kite(handler: _Kite) -> ZerodhaKiteAdapter:
    return ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(handler))


DAY_CANDLES = [
    ["2026-10-14T00:00:00+0530", 1100.0, 1120.0, 1090.0, 1110.0, 1000],
    ["2026-10-15T00:00:00+0530", 1110.0, 1130.0, 1105.0, 1125.0, 2000, 0],
]


async def test_kite_daily_history_is_requested_by_token_and_parsed_oldest_first():
    handler = _Kite({"408065": list(reversed(DAY_CANDLES))})  # even if the API order flipped

    candles = await _kite(handler).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), date(2026, 10, 15)
    )

    assert [c.close for c in candles] == [1110.0, 1125.0]
    request = handler.history_requests()[0]
    assert request.url.path == "/instruments/historical/408065/day"
    assert dict(request.url.params) == {
        "from": "2026-10-14 00:00:00",
        "to": "2026-10-15 23:59:59",
        "oi": "1",
    }


@pytest.mark.parametrize("name", ["INFY", "infy", "  Infy ", "NSE:INFY"])
async def test_a_symbol_resolves_whatever_its_case_or_exchange_prefix(name):
    handler = _Kite({"408065": DAY_CANDLES})

    await _kite(handler).get_historical_candles(name, "day", date(2026, 10, 14), date(2026, 10, 15))

    assert handler.history_requests()[0].url.path.split("/")[3] == "408065"


@pytest.mark.parametrize(
    ("name", "token"),
    [("NIFTY", "256265"), ("BANKNIFTY", "260105"), ("NIFTY 50", "256265")],
)
async def test_an_index_short_name_resolves_to_the_indexs_own_token(name, token):
    handler = _Kite({token: DAY_CANDLES})

    await _kite(handler).get_historical_candles(name, "day", date(2026, 10, 14), date(2026, 10, 15))

    assert handler.history_requests()[0].url.path.split("/")[3] == token


async def test_the_equity_row_wins_over_a_lookalike_and_another_series():
    handler = _Kite({"408065": DAY_CANDLES})
    adapter = _kite(handler)

    assert await adapter.has_instrument("INFY")
    assert await adapter.has_instrument("INFYBEES")
    await adapter.get_historical_candles("INFY", "day", date(2026, 10, 14), date(2026, 10, 15))

    assert handler.history_requests()[0].url.path.split("/")[3] == "408065"  # not INFY-BE's 111111


async def test_an_unknown_symbol_is_a_404_instrument_error_and_has_instrument_says_no():
    handler = _Kite()
    adapter = _kite(handler)

    assert await adapter.has_instrument("NOPE") is False
    with pytest.raises(InstrumentNotFoundError) as caught:
        await adapter.get_historical_candles("NOPE", "day", date(2026, 10, 14), date(2026, 10, 15))

    assert caught.value.status_code == 404 and caught.value.broker == "zerodha"
    assert isinstance(caught.value, BrokerRequestError)
    assert handler.history_requests() == []  # nothing was asked for an unknown instrument


async def test_a_symbol_that_only_trades_in_another_series_is_not_a_listed_equity():
    adapter = _kite(_Kite())

    assert await adapter.has_instrument("ONLYBE") is False  # type BE, never EQ


async def test_a_repeated_tradingsymbol_resolves_to_its_first_listing():
    handler = _Kite({"333333": DAY_CANDLES, "444444": DAY_CANDLES})

    await _kite(handler).get_historical_candles(
        "DUPSYM", "day", date(2026, 10, 14), date(2026, 10, 15)
    )

    assert handler.history_requests()[0].url.path.split("/")[3] == "333333"


async def test_the_instrument_dump_is_downloaded_once_and_shared():
    handler = _Kite({"408065": DAY_CANDLES, "738561": DAY_CANDLES})
    adapter = _kite(handler)

    await adapter.get_historical_candles("INFY", "day", date(2026, 10, 14), date(2026, 10, 15))
    await adapter.get_historical_candles("RELIANCE", "day", date(2026, 10, 14), date(2026, 10, 15))
    await _kite(handler).has_instrument("INFY")  # a different adapter instance

    assert len([r for r in handler.requests if r.url.path == "/instruments/NSE"]) == 1


async def test_a_long_daily_range_is_fetched_in_spans_without_gaps_or_overlap():
    handler = _Kite({"408065": []})
    start, end = date(2020, 1, 1), date(2026, 10, 15)  # 2480 days: more than one 1900-day span

    await _kite(handler).get_historical_candles("INFY", "day", start, end)

    spans = [
        (r.url.params["from"][:10], r.url.params["to"][:10]) for r in handler.history_requests()
    ]
    assert len(spans) == 2
    assert spans[0][0] == "2020-01-01" and spans[-1][1] == "2026-10-15"
    first_end = date.fromisoformat(spans[0][1])
    assert date.fromisoformat(spans[1][0]) == first_end + timedelta(days=1)  # contiguous


async def test_minute_history_is_spanned_at_55_days_and_deduplicated_across_spans():
    boundary = ["2026-09-01T09:15:00+0530", 1, 2, 0.5, 1.5, 10]
    handler = _Kite({"408065": [boundary]})  # the same candle comes back for every span

    candles = await _kite(handler).get_historical_candles(
        "INFY", "minute", date(2026, 8, 1), date(2026, 10, 15)
    )

    assert len(handler.history_requests()) == 2  # 76 days -> 55 + 21
    assert len(candles) == 1  # not duplicated


async def test_an_end_before_the_start_is_refused():
    with pytest.raises(ValueError, match="before start"):
        await _kite(_Kite()).get_historical_candles(
            "INFY", "day", date(2026, 10, 15), date(2026, 10, 14)
        )


async def test_a_historical_permission_error_is_a_request_error_not_an_outage():
    def denied(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/instruments/NSE":
            return httpx.Response(200, text=NSE_DUMP)
        return httpx.Response(403, json={"message": "Insufficient permission"})

    adapter = ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(denied))

    with pytest.raises(BrokerRequestError) as caught:
        await adapter.get_historical_candles("INFY", "day", date(2026, 10, 14), date(2026, 10, 15))

    assert caught.value.status_code == 403


# ---- Upstox ----

INFY_KEY = "NSE_EQ|INE009A01021"


class _Resolver:
    async def resolve(self, symbol: str) -> str:
        if "|" in symbol:
            return symbol
        if symbol.upper() != "INFY":
            raise UpstoxInstrumentNotFoundError(symbol)
        return INFY_KEY


class _Upstox:
    """A recording stand-in for api.upstox.com's candle endpoints."""

    def __init__(self, *, daily=None, intraday=None, per_day=None):
        self.requests: list[str] = []
        self.daily = daily or []
        self.intraday = intraday or []
        self.per_day = per_day or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if "/historical-candle/intraday/" in path:
            candles = self.intraday
        elif "/1minute/" in path:
            candles = self.per_day.get(path.split("/")[-1], [])
        else:
            candles = self.daily
        return httpx.Response(200, json={"status": "success", "data": {"candles": candles}})


def _upstox(handler: _Upstox, *, now: datetime) -> UpstoxAdapter:
    return UpstoxAdapter(
        _CREDENTIALS,
        transport=httpx.MockTransport(handler),
        instruments=_Resolver(),
        clock=lambda: now,
    )


# Friday 2026-10-16 after the close (16:30 IST) and during the session (10:30 IST).
AFTER_CLOSE = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
DURING_SESSION = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 16)

UPSTOX_DAILY = [  # newest first, exactly as the API sends it
    ["2026-10-15T00:00:00+05:30", 1099.0, 1126.7, 1095.8, 1110.0, 7000, 0],
    ["2026-10-14T00:00:00+05:30", 1090.0, 1100.0, 1080.0, 1099.0, 6000, 0],
]
TODAY_MINUTES = [  # newest first
    ["2026-10-16T15:29:00+05:30", 1161.2, 1161.2, 1161.2, 1161.2, 50, 0],
    ["2026-10-16T09:16:00+05:30", 1130.0, 1140.0, 1125.0, 1135.0, 200, 0],
    ["2026-10-16T09:15:00+05:30", 1127.5, 1131.0, 1122.0, 1130.95, 100, 0],
]


async def test_upstox_daily_history_uses_the_instrument_key_and_comes_back_oldest_first():
    handler = _Upstox(daily=UPSTOX_DAILY)

    candles = await _upstox(handler, now=DURING_SESSION).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), date(2026, 10, 15)
    )

    assert [c.close for c in candles] == [1099.0, 1110.0]
    assert handler.requests == [f"/v2/historical-candle/{INFY_KEY}/day/2026-10-15/2026-10-14"]


async def test_todays_daily_bar_is_composed_from_the_intraday_minutes_once_the_session_is_over():
    handler = _Upstox(daily=UPSTOX_DAILY, intraday=TODAY_MINUTES)

    candles = await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), TODAY
    )

    assert len(candles) == 3
    today = candles[-1]
    assert today.timestamp == datetime(2026, 10, 16, tzinfo=IST)
    assert (today.open, today.high, today.low, today.close) == (1127.5, 1161.2, 1122.0, 1161.2)
    assert today.volume == 350  # first open, highest high, lowest low, last close, summed volume


async def test_a_partial_day_is_never_returned_as_todays_daily_bar():
    handler = _Upstox(daily=UPSTOX_DAILY, intraday=TODAY_MINUTES)

    candles = await _upstox(handler, now=DURING_SESSION).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), TODAY
    )

    assert len(candles) == 2  # yesterday is the latest *final* bar
    assert not any("/intraday/" in path for path in handler.requests)  # not even asked


async def test_todays_bar_is_not_composed_when_the_range_ends_before_today():
    handler = _Upstox(daily=UPSTOX_DAILY, intraday=TODAY_MINUTES)

    candles = await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), date(2026, 10, 15)
    )

    assert len(candles) == 2
    assert not any("/intraday/" in path for path in handler.requests)


async def test_todays_bar_is_not_duplicated_if_the_api_already_returned_it():
    todays_own = ["2026-10-16T00:00:00+05:30", 1127.5, 1161.2, 1122.0, 1161.2, 350, 0]
    handler = _Upstox(daily=[todays_own, *UPSTOX_DAILY], intraday=TODAY_MINUTES)

    candles = await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), TODAY
    )

    assert len(candles) == 3
    assert not any("/intraday/" in path for path in handler.requests)


async def test_a_session_with_no_intraday_data_adds_no_bar():
    handler = _Upstox(daily=UPSTOX_DAILY, intraday=[])

    candles = await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), TODAY
    )

    assert len(candles) == 2


async def test_minute_history_is_one_call_per_day_with_today_from_the_intraday_endpoint():
    handler = _Upstox(
        intraday=TODAY_MINUTES,
        per_day={"2026-10-15": [["2026-10-15T09:15:00+05:30", 1, 2, 0.5, 1.5, 10, 0]]},
    )

    candles = await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
        "INFY", "minute", date(2026, 10, 15), TODAY
    )

    assert handler.requests == [
        f"/v2/historical-candle/{INFY_KEY}/1minute/2026-10-15/2026-10-15",
        f"/v2/historical-candle/intraday/{INFY_KEY}/1minute",
    ]
    assert [c.timestamp.astimezone(IST).hour for c in candles] == [9, 9, 9, 15]
    assert candles == sorted(candles, key=lambda c: c.timestamp)


async def test_minute_history_over_a_month_is_refused_before_any_call():
    handler = _Upstox()

    with pytest.raises(ValueError, match="limited to 31 days"):
        await _upstox(handler, now=AFTER_CLOSE).get_historical_candles(
            "INFY", "minute", date(2026, 9, 1), date(2026, 10, 15)
        )

    assert handler.requests == []


async def test_upstox_unknown_symbols_and_has_instrument():
    adapter = _upstox(_Upstox(), now=AFTER_CLOSE)

    assert await adapter.has_instrument("INFY") is True
    assert await adapter.has_instrument("NOPE") is False
    with pytest.raises(InstrumentNotFoundError) as caught:
        await adapter.get_historical_candles("NOPE", "day", date(2026, 10, 14), date(2026, 10, 15))
    assert caught.value.broker == "upstox" and caught.value.status_code == 404


async def test_upstox_end_before_start_is_refused():
    with pytest.raises(ValueError, match="before start"):
        await _upstox(_Upstox(), now=AFTER_CLOSE).get_historical_candles(
            "INFY", "day", date(2026, 10, 15), date(2026, 10, 14)
        )


async def test_candles_come_back_oldest_first_whatever_order_the_api_used():
    shuffled = [DAY_CANDLES[1], DAY_CANDLES[0], ["2026-10-16T00:00:00+0530", 1, 2, 0.5, 1.5, 1]]
    handler = _Kite({"408065": shuffled})

    candles = await _kite(handler).get_historical_candles(
        "INFY", "day", date(2026, 10, 14), date(2026, 10, 16)
    )

    assert [c.timestamp.date().isoformat() for c in candles] == [
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
    ]
