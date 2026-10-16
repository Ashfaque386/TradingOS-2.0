"""The symbol -> `instrument_key` directory for Upstox (src/brokers/upstox_instruments.py).

Upstox refuses a bare trading symbol (`UDAPI1087`) and wants `NSE_EQ|<ISIN>`. The directory
downloads Upstox's public instrument file; here that download is an `httpx.MockTransport`, so
nothing leaves the machine.
"""

import asyncio
import gzip
import json

import httpx
import pytest

from src.brokers.base import BrokerAuthError, BrokerRequestError
from src.brokers.upstox_instruments import (
    INSTRUMENTS_URL,
    UpstoxInstrumentNotFoundError,
    UpstoxInstruments,
    build_symbol_index,
    parse_instrument_file,
)
from src.engine.risk.circuit_breaker import BrokerServerError

ROWS = [
    # Listed *before* the real cash-equity rows, so that only the segment/type test -- not the
    # "first row wins" rule -- keeps them out of the map.
    {
        "segment": "NSE_EQ",
        "instrument_type": "BE",
        "instrument_key": "NSE_EQ|INE009A01021-BE",
        "trading_symbol": "INFY",
    },
    {
        "segment": "BSE_EQ",
        "instrument_type": "EQ",
        "instrument_key": "BSE_EQ|INE009A01021",
        "trading_symbol": "INFY",
    },
    {
        "segment": "NSE_EQ",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE009A01021",
        "trading_symbol": "INFY",
        "name": "INFOSYS LIMITED",
    },
    {
        "segment": "NSE_EQ",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE002A01018",
        "trading_symbol": "RELIANCE",
        "name": "RELIANCE INDUSTRIES LTD",
    },
    # Derivatives and anything else in the NSE file.
    {
        "segment": "NSE_FO",
        "instrument_type": "FUT",
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "INFY26OCTFUT",
    },
    {
        "segment": "NSE_INDEX",
        "instrument_type": "INDEX",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "trading_symbol": "NIFTY",
        "name": "Nifty 50",
    },
    {
        "segment": "NSE_INDEX",
        "instrument_type": "INDEX",
        "instrument_key": "NSE_INDEX|Nifty Bank",
        "trading_symbol": "BANKNIFTY",
        "name": "Nifty Bank",
    },
    {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "NOKEY"},  # no key: skipped
]


def _file(rows=ROWS) -> bytes:
    return gzip.compress(json.dumps(rows).encode())


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Server:
    """A counting stand-in for assets.upstox.com."""

    def __init__(self, response=None):
        self.calls = 0
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.response = response or httpx.Response(200, content=_file())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.urls.append(str(request.url))
        self.headers.append(dict(request.headers))
        return self.response


def _directory(server, clock=None, **kwargs) -> UpstoxInstruments:
    return UpstoxInstruments(
        transport=httpx.MockTransport(server), clock=clock or _Clock(), **kwargs
    )


# ---- what goes into the map ----


def test_cash_equities_are_indexed_by_trading_symbol_and_only_the_eq_series():
    index = build_symbol_index(ROWS)

    assert index.equities == {
        "INFY": "NSE_EQ|INE009A01021",
        "RELIANCE": "NSE_EQ|INE002A01018",
    }  # the BE row, the future and the keyless row are not in it


def test_indices_are_indexed_by_name_and_by_trading_symbol():
    index = build_symbol_index(ROWS)

    assert index.indices == {
        "NIFTY 50": "NSE_INDEX|Nifty 50",
        "NIFTY": "NSE_INDEX|Nifty 50",
        "NIFTY BANK": "NSE_INDEX|Nifty Bank",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    }


def test_the_first_row_for_a_symbol_wins_not_the_last():
    rows = [
        {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|A",
         "trading_symbol": "DUP"},
        {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|B",
         "trading_symbol": "DUP"},
    ]  # fmt: skip

    assert build_symbol_index(rows).equities == {"DUP": "NSE_EQ|A"}


def test_the_file_may_be_gzipped_or_already_plain_json():
    assert parse_instrument_file(_file()).equities["INFY"] == "NSE_EQ|INE009A01021"
    plain = json.dumps(ROWS).encode()
    assert parse_instrument_file(plain).equities["INFY"] == "NSE_EQ|INE009A01021"


def test_a_file_that_is_not_a_list_is_refused():
    with pytest.raises(ValueError, match="not a JSON list"):
        parse_instrument_file(json.dumps({"a": 1}).encode())


# ---- resolving ----


async def test_a_symbol_resolves_case_insensitively_to_its_key():
    directory = _directory(_Server())

    assert await directory.resolve("INFY") == "NSE_EQ|INE009A01021"
    assert await directory.resolve("reliance") == "NSE_EQ|INE002A01018"
    assert await directory.resolve("  Infy ") == "NSE_EQ|INE009A01021"


async def test_an_index_name_resolves_and_an_equity_wins_a_clash():
    rows = ROWS + [
        {
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE999Z01010",
            "trading_symbol": "NIFTY",
        }
    ]
    directory = _directory(_Server(httpx.Response(200, content=_file(rows))))

    assert await directory.resolve("BANKNIFTY") == "NSE_INDEX|Nifty Bank"
    assert await directory.resolve("Nifty 50") == "NSE_INDEX|Nifty 50"
    assert await directory.resolve("NIFTY") == "NSE_EQ|INE999Z01010"  # equities are tried first


async def test_a_symbol_that_is_already_a_key_is_returned_untouched_without_a_download():
    server = _Server()
    directory = _directory(server)

    assert await directory.resolve("NSE_EQ|INE848E01016") == "NSE_EQ|INE848E01016"
    assert await directory.resolve(" NSE_INDEX|Nifty 50 ") == "NSE_INDEX|Nifty 50"
    assert server.calls == 0


async def test_an_unknown_symbol_is_a_404_request_error_naming_it():
    directory = _directory(_Server())

    with pytest.raises(UpstoxInstrumentNotFoundError) as caught:
        await directory.resolve("NOPE123")

    assert caught.value.status_code == 404
    assert caught.value.symbol == "NOPE123"
    assert "NOPE123" in str(caught.value)
    assert isinstance(caught.value, BrokerRequestError)
    # A request mistake: it must never count against the circuit breaker.
    assert not isinstance(caught.value, BrokerAuthError | BrokerServerError)


async def test_a_future_or_other_series_is_not_resolved_as_the_cash_equity():
    directory = _directory(_Server())

    with pytest.raises(UpstoxInstrumentNotFoundError):
        await directory.resolve("INFY26OCTFUT")


# ---- the download and the cache ----


async def test_the_public_file_is_fetched_once_and_without_credentials():
    server = _Server()
    directory = _directory(server)

    await directory.resolve("INFY")
    await directory.resolve("RELIANCE")
    await directory.resolve("NIFTY")

    assert server.calls == 1
    assert server.urls == [INSTRUMENTS_URL]
    assert INSTRUMENTS_URL.startswith("https://assets.upstox.com/")
    assert "authorization" not in {k.lower() for k in server.headers[0]}  # a public file


async def test_concurrent_first_lookups_share_one_download():
    class _Slow(_Server):
        pass

    server = _Slow()
    directory = _directory(server)

    results = await asyncio.gather(*[directory.resolve("INFY") for _ in range(10)])

    assert set(results) == {"NSE_EQ|INE009A01021"}
    assert server.calls == 1


async def test_a_cached_lookup_does_not_wait_for_a_refresh_in_progress():
    """Only a stale or empty cache takes the lock: while one caller is downloading, everyone
    whose answer is already cached must not queue behind it."""
    directory = _directory(_Server())
    await directory.resolve("INFY")

    async with directory._lock:  # a refresh is "in progress"
        key = await asyncio.wait_for(directory.resolve("INFY"), timeout=1)

    assert key == "NSE_EQ|INE009A01021"


async def test_the_list_is_refreshed_after_its_lifetime_and_not_before():
    clock = _Clock()
    server = _Server()
    directory = _directory(server, clock, ttl_seconds=100)

    await directory.resolve("INFY")
    clock.now += 99
    await directory.resolve("INFY")
    assert server.calls == 1  # still inside the window

    clock.now += 2  # 101 s after the load
    await directory.resolve("INFY")
    assert server.calls == 2


async def test_a_refresh_sees_a_newly_listed_symbol():
    clock = _Clock()
    server = _Server()
    directory = _directory(server, clock, ttl_seconds=100)
    with pytest.raises(UpstoxInstrumentNotFoundError):
        await directory.resolve("NEWCO")

    server.response = httpx.Response(
        200,
        content=_file(
            ROWS
            + [
                {
                    "segment": "NSE_EQ",
                    "instrument_type": "EQ",
                    "instrument_key": "NSE_EQ|INE000N01010",
                    "trading_symbol": "NEWCO",
                }
            ]
        ),
    )
    clock.now += 101

    assert await directory.resolve("NEWCO") == "NSE_EQ|INE000N01010"


# ---- when the download goes wrong ----


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(503, text="down"), BrokerServerError),
        (httpx.Response(404, text="gone"), BrokerRequestError),
        (httpx.Response(200, content=b"this is not gzip or json"), BrokerServerError),
        (httpx.Response(200, content=gzip.compress(b"{truncated")), BrokerServerError),
    ],
)
async def test_a_failed_first_download_raises_and_is_retried_next_time(response, expected):
    server = _Server(response)
    directory = _directory(server)

    with pytest.raises(expected):
        await directory.resolve("INFY")

    server.response = httpx.Response(200, content=_file())  # the next attempt works
    assert await directory.resolve("INFY") == "NSE_EQ|INE009A01021"
    assert server.calls == 2


async def test_an_unreachable_host_is_an_outage_that_names_the_broker_and_the_file():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    directory = UpstoxInstruments(transport=httpx.MockTransport(handler), clock=_Clock())

    with pytest.raises(BrokerServerError) as caught:
        await directory.resolve("INFY")

    assert str(caught.value).startswith("upstox GET NSE.json.gz: unreachable (")


async def test_a_failed_refresh_keeps_serving_the_old_list_and_waits_before_retrying():
    clock = _Clock()
    server = _Server()
    directory = _directory(server, clock, ttl_seconds=100)
    await directory.resolve("INFY")

    server.response = httpx.Response(503, text="down")
    clock.now += 101
    assert await directory.resolve("INFY") == "NSE_EQ|INE009A01021"  # stale, but better than none
    assert server.calls == 2

    clock.now += 60  # inside the back-off after the failure: no hammering
    assert await directory.resolve("INFY") == "NSE_EQ|INE009A01021"
    assert server.calls == 2

    clock.now += 600  # past it: tries again
    await directory.resolve("INFY")
    assert server.calls == 3
