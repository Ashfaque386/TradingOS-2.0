"""Option contracts: one name for every broker, and each broker's contract list (phase 5).

`NFO:NIFTY|261027|24500|CE` names a contract the same way to Kite and Upstox (underlying, expiry
as YYMMDD, strike, CE/PE). Each adapter maps it to its own instrument through its instrument master
by underlying, expiry date, strike and type -- exact for weekly and monthly expiries alike, which
Kite spells differently. Upstox rows here are shaped like the real `NSE.json.gz` (37,699 option
rows; `strike_price`, `weekly`, `expiry` in epoch milliseconds at 23:59:59 IST).
"""

import json
from datetime import date

import httpx
import pytest

from src.brokers.base import (
    BrokerCredentials,
    BrokerQuoteUnavailableError,
    OptionContract,
    option_symbol,
    parse_option_symbol,
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
    ("underlying", "expiry", "strike", "kind", "name"),
    [
        ("NIFTY", date(2026, 10, 27), 24500.0, "CE", "NFO:NIFTY|261027|24500|CE"),
        ("nifty", date(2026, 10, 19), 24500, "pe", "NFO:NIFTY|261019|24500|PE"),
        ("RELIANCE", date(2026, 11, 24), 1237.5, "CE", "NFO:RELIANCE|261124|1237.5|CE"),
        ("M&M", date(2027, 1, 28), 3100.0, "PE", "NFO:M&M|270128|3100|PE"),
        ("BAJAJ-AUTO", date(2026, 12, 29), 9800.0, "CE", "NFO:BAJAJ-AUTO|261229|9800|CE"),
        ("NIFTYNXT50", date(2026, 10, 27), 69000.0, "CE", "NFO:NIFTYNXT50|261027|69000|CE"),
    ],
)
def test_an_option_is_named_underlying_expiry_strike_and_type(
    underlying, expiry, strike, kind, name
):
    assert option_symbol(underlying, expiry, strike, kind) == name


@pytest.mark.parametrize("kind", ["XX", "", "CALL"])
def test_an_option_is_a_call_or_a_put_and_nothing_else(kind):
    with pytest.raises(ValueError):
        option_symbol("NIFTY", date(2026, 10, 27), 24500.0, kind)


@pytest.mark.parametrize(
    ("underlying", "expiry", "strike", "kind"),
    [
        ("NIFTY", date(2026, 10, 27), 24500.0, "CE"),
        ("NIFTY", date(2026, 10, 19), 20050.0, "PE"),
        ("RELIANCE", date(2026, 11, 24), 1237.5, "CE"),
        ("M&M", date(2027, 1, 28), 3100.0, "PE"),
        ("BAJAJ-AUTO", date(2026, 12, 29), 9800.0, "CE"),
        ("NIFTYNXT50", date(2026, 10, 27), 69000.0, "CE"),  # an underlying that ends in digits
    ],
)
def test_the_name_parses_back_exactly(underlying, expiry, strike, kind):
    name = option_symbol(underlying, expiry, strike, kind)

    assert parse_option_symbol(name) == (underlying, expiry, strike, kind)


@pytest.mark.parametrize(
    "name",
    [
        "INFY",
        "NIFTY",
        "NFO:NIFTY26OCTFUT",  # a future
        "NFO:NIFTY|261327|24500|CE",  # month 13
        "NFO:NIFTY|261032|24500|CE",  # day 32
        "NFO:NIFTY|261027|24500|XX",
        "NFO:NIFTY|2610|24500|CE",
        "NFO:NIFTY|261027||CE",
        "",
    ],
)
def test_anything_that_is_not_an_option_contract_does_not_parse(name):
    assert parse_option_symbol(name) is None


# ---- Zerodha's NFO dump ----

_COLUMNS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange"
)
NFO_CSV = f"""{_COLUMNS}
1,1,NIFTY26OCT24500CE,NIFTY,0,2026-10-27,24500,0.05,65,CE,NFO-OPT,NFO
2,2,NIFTY26OCT24500PE,NIFTY,0,2026-10-27,24500,0.05,65,PE,NFO-OPT,NFO
3,3,NIFTY26N0324500CE,NIFTY,0,2026-11-03,24500,0.05,65,CE,NFO-OPT,NFO
4,4,NIFTY26OCTFUT,NIFTY,0,2026-10-27,0,10,65,FUT,NFO-FUT,NFO
5,5,BANKNIFTY26OCT55000CE,BANKNIFTY,0,2026-10-27,55000,0.05,30,CE,NFO-OPT,NFO
6,6,NIFTY26OCT24550CE,NIFTY,0,2026-10-27,24550,0.05,0,CE,NFO-OPT,NFO
7,7,NIFTY26OCT24600CE,NIFTY,0,not-a-date,24600,0.05,65,CE,NFO-OPT,NFO
"""


@pytest.fixture(autouse=True)
def _fresh_kite_caches():
    from src.brokers import zerodha

    zerodha._nfo_instruments_cache = None
    zerodha._nfo_option_index = None
    yield
    zerodha._nfo_instruments_cache = None
    zerodha._nfo_option_index = None


def _kite(handler=None, quotes=None, seen=None) -> ZerodhaKiteAdapter:
    def default(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/instruments/NFO":
            return httpx.Response(200, text=NFO_CSV)
        if request.url.path == "/quote":
            if seen is not None:
                seen.append(request.url.params.get_list("i"))
            return httpx.Response(200, json={"data": quotes or {}})
        raise AssertionError(request.url.path)

    return ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(handler or default),
    )


async def test_kite_lists_the_underlyings_options_across_expiries_with_the_dumps_lot_size():
    contracts = await _kite().get_option_contracts("nifty")

    assert sorted((c.symbol, c.lot_size, c.tick_size) for c in contracts) == [
        ("NFO:NIFTY|261027|24500|CE", 65, 0.05),
        ("NFO:NIFTY|261027|24500|PE", 65, 0.05),
        ("NFO:NIFTY|261103|24500|CE", 65, 0.05),  # a weekly, whatever Kite's spelling of it
    ]  # no future, no BANKNIFTY, no zero-lot row, no unreadable date


async def test_kite_has_no_options_for_an_underlying_without_any():
    assert await _kite().get_option_contracts("RELIANCE") == []


async def test_kite_quotes_an_option_by_its_own_tradingsymbol_found_through_the_shared_name():
    seen: list[list[str]] = []
    row = {"last_price": 120.5, "oi": 9000, "depth": {"buy": [], "sell": []}}
    adapter = _kite(quotes={"NFO:NIFTY26N0324500CE": row}, seen=seen)

    quotes = await adapter.get_quotes(["NFO:NIFTY|261103|24500|CE"])

    assert seen == [["NFO:NIFTY26N0324500CE"]]  # the weekly's own odd spelling, not ours
    assert quotes["NFO:NIFTY|261103|24500|CE"].last_price == 120.5  # keyed by the shared name
    assert quotes["NFO:NIFTY|261103|24500|CE"].open_interest == 9000.0


async def test_kite_leaves_out_an_option_its_dump_does_not_list_and_never_asks_about_it():
    seen: list[list[str]] = []
    adapter = _kite(quotes={}, seen=seen)

    quotes = await adapter.get_quotes(["NFO:NIFTY|261027|99999|CE"])

    assert quotes == {} and seen == []


async def test_kite_mixes_options_and_equities_in_one_request():
    seen: list[list[str]] = []
    adapter = _kite(
        quotes={
            "NFO:NIFTY26OCT24500CE": {"last_price": 100.0},
            "NSE:INFY": {"last_price": 1500.0},
        },
        seen=seen,
    )

    quotes = await adapter.get_quotes(["INFY", "NFO:NIFTY|261027|24500|CE"])

    assert seen == [["NSE:INFY", "NFO:NIFTY26OCT24500CE"]]
    assert set(quotes) == {"INFY", "NFO:NIFTY|261027|24500|CE"}


async def test_kite_single_quote_of_an_unlisted_option_is_unavailable():
    with pytest.raises(BrokerQuoteUnavailableError):
        await _kite().get_quote("NFO:NIFTY|261027|99999|CE")


async def test_kite_single_quote_of_a_listed_option_uses_its_tradingsymbol():
    seen: list[list[str]] = []
    adapter = _kite(quotes={"NFO:NIFTY26OCT24500PE": {"last_price": 88.0}}, seen=seen)

    quote = await adapter.get_quote("NFO:NIFTY|261027|24500|PE")

    assert seen == [["NFO:NIFTY26OCT24500PE"]] and quote.last_price == 88.0


async def test_the_option_index_is_reused_while_the_dump_it_came_from_is():
    adapter = _kite()

    first = await adapter._option_index()
    second = await adapter._option_index()

    assert first is second  # not rebuilt from ~38,000 rows on every quote


async def test_the_option_index_is_built_once_per_dump():
    downloads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        downloads.append(request.url.path)
        return httpx.Response(200, text=NFO_CSV)

    adapter = _kite(handler)
    await adapter.get_option_contracts("NIFTY")
    await adapter.get_option_contracts("BANKNIFTY")

    assert downloads == ["/instruments/NFO"]


# ---- Upstox's instrument file ----


def _row(underlying, expiry_ms, strike, kind, key, lot=65, *, weekly=False):
    return {
        "instrument_key": key,
        "trading_symbol": f"{underlying} {int(strike)} {kind}",
        "name": underlying,
        "underlying_symbol": underlying,
        "expiry": expiry_ms,
        "strike_price": strike,
        "lot_size": lot,
        "tick_size": 5.0,
        "weekly": weekly,
        "segment": "NSE_FO",
        "instrument_type": kind,
    }


# 2026-10-27 and 2026-10-19 at 23:59:59 IST, as the real file stamps them.
OCT27_MS, OCT19_MS = 1793125799000, 1792434599000

ROWS = [
    _row("NIFTY", OCT27_MS, 24500.0, "CE", "NSE_FO|1"),
    _row("NIFTY", OCT27_MS, 24500.0, "PE", "NSE_FO|2"),
    _row("NIFTY", OCT19_MS, 24500.0, "CE", "NSE_FO|3", weekly=True),
    _row("RELIANCE", OCT27_MS, 1237.5, "CE", "NSE_FO|4", lot=500),
    _row("ZEROLOT", OCT27_MS, 100.0, "CE", "NSE_FO|5", lot=0),
    {**_row("NOSTRIKE", OCT27_MS, 100.0, "CE", "NSE_FO|6"), "strike_price": None},
    {**_row("FUTONLY", OCT27_MS, 0.0, "FUT", "NSE_FO|7")},
]


def test_upstox_rows_become_contracts_with_the_ist_expiry_date_and_the_shared_name():
    index = build_symbol_index(ROWS)

    contract = index.options[("NIFTY", date(2026, 10, 27), 24500.0, "CE")].contract
    assert contract == OptionContract(
        "NFO:NIFTY|261027|24500|CE", "NIFTY", date(2026, 10, 27), 24500.0, "CE", 65, 5.0
    )
    assert ("NIFTY", date(2026, 10, 19), 24500.0, "CE") in index.options  # the weekly too


def test_unusable_option_rows_are_not_indexed():
    index = build_symbol_index(ROWS)

    # No zero lot, no strike, no future masquerading as an option.
    assert {key[0] for key in index.options} == {"NIFTY", "RELIANCE"}


def _directory() -> UpstoxInstruments:
    directory = UpstoxInstruments()
    directory._index = parse_instrument_file(json.dumps(ROWS).encode())
    directory._valid_until = float("inf")
    return directory


async def test_the_shared_name_resolves_to_upstoxs_instrument_key():
    directory = _directory()

    assert await directory.resolve("NFO:NIFTY|261027|24500|CE") == "NSE_FO|1"
    assert await directory.resolve("NFO:NIFTY|261027|24500|PE") == "NSE_FO|2"
    assert await directory.resolve("NFO:NIFTY|261019|24500|CE") == "NSE_FO|3"
    assert await directory.resolve("nfo:reliance|261027|1237.5|ce") == "NSE_FO|4"


async def test_an_option_upstox_does_not_list_is_not_found():
    with pytest.raises(UpstoxInstrumentNotFoundError):
        await _directory().resolve("NFO:NIFTY|261027|99999|CE")


async def test_upstox_lists_the_underlyings_options_across_expiries():
    contracts = await _directory().options_for("nifty")

    assert sorted(c.symbol for c in contracts) == [
        "NFO:NIFTY|261019|24500|CE",
        "NFO:NIFTY|261027|24500|CE",
        "NFO:NIFTY|261027|24500|PE",
    ]
    assert await _directory().options_for("INFY") == []


async def test_both_brokers_name_the_same_contract_the_same_way():
    [kite_ce] = [
        c
        for c in await _kite().get_option_contracts("NIFTY")
        if c.expiry == date(2026, 10, 27) and c.option_type == "CE"
    ]
    upstox = await _directory().options_for("NIFTY")
    [upstox_ce] = [c for c in upstox if c.expiry == date(2026, 10, 27) and c.option_type == "CE"]

    assert kite_ce.symbol == upstox_ce.symbol  # what lets one broker back the other up
    assert (kite_ce.strike, kite_ce.lot_size) == (upstox_ce.strike, upstox_ce.lot_size)


async def test_the_upstox_adapter_quotes_an_option_by_the_shared_name():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["instrument_key"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "NSE_FO:NIFTY": {
                        "last_price": 132.5,
                        "oi": 4500,
                        "instrument_token": "NSE_FO|1",
                        "depth": {
                            "buy": [{"price": 132.0, "quantity": 650, "orders": 4}],
                            "sell": [{"price": 133.0, "quantity": 325, "orders": 2}],
                        },
                    }
                }
            },
        )

    adapter = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(handler),
        instruments=_directory(),
    )

    quotes = await adapter.get_quotes(["NFO:NIFTY|261027|24500|CE"])

    assert requested == ["NSE_FO|1"]
    assert quotes["NFO:NIFTY|261027|24500|CE"].depth_asks[0].quantity == 325
    assert len(await adapter.get_option_contracts("NIFTY")) == 3


# ---- the provider ----


class _Lister:
    def __init__(self, name, contracts=None, error=None):
        self.broker_name = name
        self._contracts = contracts or []
        self._error = error

    async def get_option_contracts(self, underlying):
        if self._error:
            raise self._error
        return self._contracts


def _contract(day=27):
    return OptionContract(
        option_symbol("NIFTY", date(2026, 10, day), 24500.0, "CE"),
        "NIFTY",
        date(2026, 10, day),
        24500.0,
        "CE",
        65,
        5.0,
    )


def _provider(tmp_path, *adapters):
    from src.data.broker_provider import BrokerMarketDataProvider

    return BrokerMarketDataProvider(lambda: list(adapters), root=tmp_path)


async def test_the_provider_takes_contracts_from_the_first_broker_that_has_any(tmp_path):
    provider = _provider(
        tmp_path,
        _Lister("zerodha", []),
        _Lister("upstox", [_contract()]),
        _Lister("x", [_contract(19)]),
    )

    assert [c.expiry for c in await provider.option_contracts("NIFTY")] == [date(2026, 10, 27)]


async def test_a_failing_broker_is_skipped_for_the_next(tmp_path):
    provider = _provider(
        tmp_path,
        _Lister("zerodha", error=ConnectionError("down")),
        _Lister("upstox", [_contract()]),
    )

    assert len(await provider.option_contracts("NIFTY")) == 1


async def test_with_nobody_listing_any_the_answer_is_empty_not_invented(tmp_path):
    assert await _provider(tmp_path, _Lister("zerodha", [])).option_contracts("NIFTY") == []


async def test_with_no_broker_connected_the_provider_will_not_answer(tmp_path):
    from src.data.broker_provider import NoRealDataSourceError

    with pytest.raises(NoRealDataSourceError):
        await _provider(tmp_path).option_contracts("NIFTY")
