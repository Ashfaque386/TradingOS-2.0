"""Monthly futures: naming, the brokers' contract lists, and which contract to trade (phase 4).

One name, `NFO:NIFTY26OCTFUT`, identifies a contract to every broker; each adapter maps it to its
own instrument. The contract and its lot size come from the instrument masters (Upstox's rows are
shaped like the real `NSE.json.gz`: 653 `FUT` rows, expiry in epoch milliseconds at 23:59:59 IST).
"""

import json
from datetime import date

import httpx
import pytest

from src.brokers.base import (
    BrokerCredentials,
    FuturesContract,
    futures_symbol,
    parse_futures_symbol,
)
from src.brokers.futures_contracts import (
    NoFuturesContractError,
    resolve_contract,
    roll_due,
    select_contract,
)
from src.brokers.upstox import UpstoxAdapter
from src.brokers.upstox_instruments import (
    UpstoxInstrumentNotFoundError,
    UpstoxInstruments,
    build_symbol_index,
    parse_instrument_file,
)
from src.brokers.zerodha import ZerodhaKiteAdapter

# ---- naming ----


@pytest.mark.parametrize(
    ("underlying", "expiry", "name"),
    [
        ("NIFTY", date(2026, 10, 27), "NFO:NIFTY26OCTFUT"),
        ("reliance", date(2026, 11, 23), "NFO:RELIANCE26NOVFUT"),
        ("M&M", date(2027, 1, 28), "NFO:M&M27JANFUT"),
        ("BAJAJ-AUTO", date(2026, 12, 29), "NFO:BAJAJ-AUTO26DECFUT"),
    ],
)
def test_a_future_is_named_underlying_year_month_and_fut(underlying, expiry, name):
    assert futures_symbol(underlying, expiry) == name


@pytest.mark.parametrize(
    ("name", "parsed"),
    [
        ("NFO:NIFTY26OCTFUT", ("NIFTY", 2026, 10)),
        ("NIFTY26OCTFUT", ("NIFTY", 2026, 10)),  # Kite's bare tradingsymbol
        ("nfo:reliance26novfut", ("RELIANCE", 2026, 11)),
        ("NFO:M&M27JANFUT", ("M&M", 2027, 1)),
        ("NFO:BAJAJ-AUTO26DECFUT", ("BAJAJ-AUTO", 2026, 12)),
    ],
)
def test_a_futures_name_parses_back_to_underlying_and_month(name, parsed):
    assert parse_futures_symbol(name) == parsed


@pytest.mark.parametrize(
    "name", ["INFY", "NIFTY", "NFO:NIFTY26OCT24500CE", "NFO:NIFTY26XYZFUT", "NIFTY26OCT", ""]
)
def test_anything_that_is_not_a_monthly_future_does_not_parse(name):
    assert parse_futures_symbol(name) is None


def test_the_name_round_trips_for_every_month():
    for month in range(1, 13):
        expiry = date(2026, month, 25)
        assert parse_futures_symbol(futures_symbol("TCS", expiry)) == ("TCS", 2026, month)


# ---- choosing the contract ----


def _contract(expiry: date, lot: int = 65) -> FuturesContract:
    return FuturesContract(futures_symbol("NIFTY", expiry), "NIFTY", expiry, lot, 10.0)


OCT, NOV, DEC = date(2026, 10, 27), date(2026, 11, 23), date(2026, 12, 29)


def test_the_soonest_contract_beyond_the_roll_window_is_chosen():
    chosen = select_contract(
        [_contract(DEC), _contract(OCT), _contract(NOV)], today=date(2026, 10, 1), roll_days=3
    )

    assert chosen.expiry == OCT


def test_inside_the_roll_window_the_next_contract_is_chosen():
    contracts = [_contract(OCT), _contract(NOV), _contract(DEC)]

    # 3 days before the October expiry: October is due for a roll, so it is not the pick.
    assert select_contract(contracts, today=date(2026, 10, 24), roll_days=3).expiry == NOV
    # 4 days before: still comfortably October's.
    assert select_contract(contracts, today=date(2026, 10, 23), roll_days=3).expiry == OCT


def test_no_contract_beyond_the_window_is_none():
    assert select_contract([_contract(OCT)], today=date(2026, 10, 26), roll_days=3) is None
    assert select_contract([], today=date(2026, 10, 1), roll_days=3) is None


@pytest.mark.parametrize(
    ("today", "due"),
    [
        (date(2026, 10, 23), False),  # 4 days
        (date(2026, 10, 24), True),  # 3 days: from here
        (date(2026, 10, 27), True),  # expiry day
        (date(2026, 10, 28), True),  # after it (a roll that never happened)
    ],
)
def test_a_roll_is_due_from_roll_days_before_expiry(today, due):
    assert roll_due(OCT, today=today, roll_days=3) is due


class _Broker:
    def __init__(self, name, contracts=None, error=None):
        self.broker_name = name
        self._contracts = contracts or []
        self._error = error
        self.asked: list[str] = []

    async def get_futures_contracts(self, underlying):
        self.asked.append(underlying)
        if self._error is not None:
            raise self._error
        return self._contracts


async def test_the_first_broker_that_lists_a_usable_contract_decides():
    zerodha = _Broker("zerodha", [_contract(NOV, lot=75)])
    upstox = _Broker("upstox", [_contract(NOV, lot=65)])

    chosen = await resolve_contract(
        [zerodha, upstox], "NIFTY", today=date(2026, 10, 25), roll_days=3
    )

    assert chosen.lot_size == 75
    assert upstox.asked == []


async def test_a_broker_that_fails_is_skipped_for_the_next():
    zerodha = _Broker("zerodha", error=ConnectionError("reset"))
    upstox = _Broker("upstox", [_contract(NOV)])

    chosen = await resolve_contract(
        [zerodha, upstox], "NIFTY", today=date(2026, 10, 25), roll_days=3
    )

    assert chosen.expiry == NOV


async def test_with_no_answer_the_error_says_why_for_each_broker():
    zerodha = _Broker("zerodha", error=ConnectionError("reset"))
    upstox = _Broker("upstox", [_contract(OCT)])  # only a contract inside the window

    with pytest.raises(NoFuturesContractError) as caught:
        await resolve_contract([zerodha, upstox], "NIFTY", today=date(2026, 10, 25), roll_days=3)

    assert "zerodha: ConnectionError" in str(caught.value)
    assert "upstox: no contract expiring after 3 days" in str(caught.value)


async def test_with_no_broker_connected_a_contract_is_never_guessed():
    with pytest.raises(NoFuturesContractError, match="no broker is connected"):
        await resolve_contract([], "NIFTY", today=date(2026, 10, 25), roll_days=3)


# ---- Zerodha's NFO dump ----

_NFO_COLUMNS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange"
)
NFO_CSV = f"""{_NFO_COLUMNS}
1,1,NIFTY26NOVFUT,NIFTY,0,2026-11-23,0,10,65,FUT,NFO-FUT,NFO
2,2,NIFTY26OCTFUT,NIFTY,0,2026-10-27,0,10,65,FUT,NFO-FUT,NFO
3,3,NIFTY26OCT24500CE,NIFTY,0,2026-10-27,24500,0.05,65,CE,NFO-OPT,NFO
4,4,BANKNIFTY26OCTFUT,BANKNIFTY,0,2026-10-27,0,10,30,FUT,NFO-FUT,NFO
5,5,NIFTY26DECFUT,NIFTY,0,2026-12-29,0,10,0,FUT,NFO-FUT,NFO
6,6,NIFTY26JANFUT,NIFTY,0,not-a-date,0,10,65,FUT,NFO-FUT,NFO
"""


def _kite() -> ZerodhaKiteAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instruments/NFO"
        return httpx.Response(200, text=NFO_CSV)

    return ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )


@pytest.fixture(autouse=True)
def _fresh_kite_cache():
    from src.brokers import zerodha

    zerodha._nfo_instruments_cache = None
    yield
    zerodha._nfo_instruments_cache = None


async def test_kite_lists_the_underlyings_futures_soonest_first_with_the_dumps_lot_size():
    contracts = await _kite().get_futures_contracts("nifty")

    assert [(c.symbol, c.expiry, c.lot_size, c.tick_size) for c in contracts] == [
        ("NFO:NIFTY26OCTFUT", date(2026, 10, 27), 65, 10.0),
        ("NFO:NIFTY26NOVFUT", date(2026, 11, 23), 65, 10.0),
    ]  # no options, no BANKNIFTY, no zero-lot row, no unreadable date


async def test_kite_has_no_futures_for_an_underlying_without_any():
    assert await _kite().get_futures_contracts("RELIANCE") == []


async def test_kites_name_for_a_future_is_the_shared_name():
    """The shared name is Kite's own `NFO:` + tradingsymbol, so a Kite quote needs no mapping."""
    [first, *_] = await _kite().get_futures_contracts("NIFTY")

    assert first.symbol == "NFO:NIFTY26OCTFUT"


# ---- Upstox's instrument file ----


def _row(underlying, expiry_ms, lot, key, *, kind="FUT", segment="NSE_FO"):
    return {
        "instrument_key": key,
        "trading_symbol": f"{underlying} FUT",
        "name": underlying,
        "underlying_symbol": underlying,
        "expiry": expiry_ms,
        "lot_size": lot,
        "tick_size": 10.0,
        "segment": segment,
        "instrument_type": kind,
    }


# 2026-10-27 23:59:59 IST and 2026-11-23 23:59:59 IST, as the real file stamps them.
OCT_MS, NOV_MS = 1793125799000, 1795458599000

ROWS = [
    _row("NIFTY", NOV_MS, 65, "NSE_FO|61471"),
    _row("NIFTY", OCT_MS, 65, "NSE_FO|48704"),
    _row("RELIANCE", OCT_MS, 500, "NSE_FO|48987"),
    _row("OPTONLY", OCT_MS, 65, "NSE_FO|1", kind="CE"),  # an option is not a future
    _row("ZEROLOT", OCT_MS, 0, "NSE_FO|2"),  # a contract with no lot size is unusable
    {**_row("M&M", OCT_MS, 700, "NSE_FO|3")},
    {
        "instrument_key": "NSE_EQ|INE009A01021",
        "trading_symbol": "INFY",
        "segment": "NSE_EQ",
        "instrument_type": "EQ",
    },
]


def test_upstox_expiry_milliseconds_become_the_ist_expiry_date():
    """The file stamps 23:59:59 IST of the expiry day (18:29:59 UTC, the same date)."""
    index = build_symbol_index(ROWS)

    assert index.futures[("NIFTY", 2026, 10)].contract.expiry == date(2026, 10, 27)
    assert index.futures[("NIFTY", 2026, 11)].contract.expiry == date(2026, 11, 23)


def test_only_futures_with_a_lot_size_are_indexed():
    index = build_symbol_index(ROWS)

    assert sorted(index.futures) == [
        ("M&M", 2026, 10),
        ("NIFTY", 2026, 10),
        ("NIFTY", 2026, 11),
        ("RELIANCE", 2026, 10),
    ]
    assert index.equities == {"INFY": "NSE_EQ|INE009A01021"}  # equities unaffected


def _upstox_directory() -> UpstoxInstruments:
    directory = UpstoxInstruments()
    directory._index = parse_instrument_file(json.dumps(ROWS).encode())
    directory._valid_until = float("inf")
    return directory


async def test_the_shared_name_resolves_to_upstoxs_instrument_key():
    directory = _upstox_directory()

    assert await directory.resolve("NFO:NIFTY26OCTFUT") == "NSE_FO|48704"
    assert await directory.resolve("NFO:RELIANCE26OCTFUT") == "NSE_FO|48987"
    assert await directory.resolve("nfo:m&m26octfut") == "NSE_FO|3"
    assert await directory.resolve("INFY") == "NSE_EQ|INE009A01021"  # equities as before


async def test_a_month_upstox_does_not_list_is_not_found():
    with pytest.raises(UpstoxInstrumentNotFoundError):
        await _upstox_directory().resolve("NFO:NIFTY26DECFUT")


async def test_upstox_lists_the_underlyings_futures_soonest_first():
    contracts = await _upstox_directory().futures_for("nifty")

    assert [(c.symbol, c.lot_size) for c in contracts] == [
        ("NFO:NIFTY26OCTFUT", 65),
        ("NFO:NIFTY26NOVFUT", 65),
    ]


async def test_both_brokers_name_the_same_contract_the_same_way():
    [upstox_first, *_] = await _upstox_directory().futures_for("NIFTY")
    [kite_first, *_] = await _kite().get_futures_contracts("NIFTY")

    assert upstox_first.symbol == kite_first.symbol  # what lets one broker back the other up


async def test_the_adapter_quotes_a_future_by_the_shared_name():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["instrument_key"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "NSE_FO:NIFTY": {
                        "last_price": 24610.5,
                        "oi": 11250300,
                        "instrument_token": "NSE_FO|48704",
                        "depth": {
                            "buy": [{"price": 24610.0, "quantity": 650, "orders": 4}],
                            "sell": [{"price": 24611.0, "quantity": 325, "orders": 2}],
                        },
                    }
                }
            },
        )

    adapter = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(handler),
        instruments=_upstox_directory(),
    )

    quotes = await adapter.get_quotes(["NFO:NIFTY26OCTFUT"])

    assert requested == ["NSE_FO|48704"]
    assert quotes["NFO:NIFTY26OCTFUT"].open_interest == 11250300.0
    assert quotes["NFO:NIFTY26OCTFUT"].depth_asks[0].quantity == 325
    assert [c.lot_size for c in await adapter.get_futures_contracts("NIFTY")] == [65, 65]


# ---- the market-data provider's futures helpers ----


class _Quoting:
    def __init__(self, name, price=None, error=None, contracts=None):
        self.broker_name = name
        self._price = price
        self._error = error
        self._contracts = contracts or []

    async def get_quote(self, symbol):
        if self._error:
            raise self._error
        from datetime import UTC, datetime

        from src.brokers.base import BrokerQuote

        return BrokerQuote(symbol, self._price, None, None, datetime.now(UTC))

    async def get_futures_contracts(self, underlying):
        return self._contracts


def _provider(tmp_path, *adapters):
    from src.data.broker_provider import BrokerMarketDataProvider

    return BrokerMarketDataProvider(lambda: list(adapters), root=tmp_path)


async def test_the_provider_resolves_a_contract_from_the_connected_brokers(tmp_path):
    provider = _provider(tmp_path, _Quoting("zerodha", contracts=[_contract(NOV, lot=75)]))

    contract = await provider.futures_contract("NIFTY", today=date(2026, 10, 25), roll_days=3)

    assert (contract.expiry, contract.lot_size) == (NOV, 75)


async def test_with_no_broker_connected_the_provider_will_not_name_a_contract(tmp_path):
    from src.data.broker_provider import NoRealDataSourceError

    with pytest.raises(NoRealDataSourceError):
        await _provider(tmp_path).futures_contract("NIFTY", today=date(2026, 10, 25), roll_days=3)


async def test_an_indicative_price_comes_from_the_first_broker_that_has_one(tmp_path):
    provider = _provider(
        tmp_path,
        _Quoting("zerodha", error=ConnectionError("down")),
        _Quoting("upstox", price=24_010.5),
    )

    assert await provider.indicative_price("NFO:NIFTY26OCTFUT") == 24_010.5


async def test_with_nobody_quoting_the_indicative_price_is_none_not_zero(tmp_path):
    provider = _provider(tmp_path, _Quoting("zerodha", error=ConnectionError("down")))

    assert await provider.indicative_price("NFO:NIFTY26OCTFUT") is None
