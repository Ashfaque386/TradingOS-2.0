"""Batched quotes with the 5-level book and open interest (real-market-data plan, phase 3).

Both brokers answer a quote request with `last_price`, a `depth` of `buy`/`sell` levels and (for a
derivative) `oi`. `get_quotes` asks for many symbols in one request; a symbol the broker has no
price for is left out of the answer -- never a zero-priced quote.
"""

import httpx
import pytest

from src.brokers.base import (
    QUOTE_BATCH_SIZE,
    BrokerCredentials,
    BrokerQuoteUnavailableError,
    DepthLevel,
    quote_from_row,
)
from src.brokers.upstox import UpstoxAdapter
from src.brokers.upstox_instruments import UpstoxInstrumentNotFoundError
from src.brokers.zerodha import ZerodhaKiteAdapter

_CREDENTIALS = BrokerCredentials(api_key="k", access_token="t")

INFY_ROW = {
    "last_price": 1503.25,
    "oi": 0,
    "depth": {
        "buy": [
            {"price": 1503.0, "quantity": 120, "orders": 3},
            {"price": 1502.95, "quantity": 300, "orders": 5},
            {"price": 0, "quantity": 0, "orders": 0},
        ],
        "sell": [
            {"price": 1503.3, "quantity": 80, "orders": 2},
            {"price": 1503.35, "quantity": 500, "orders": 7},
        ],
    },
}
NIFTY_FUT_ROW = {
    "last_price": 24610.5,
    "oi": 11250300,
    "depth": {
        "buy": [{"price": 24610.0, "quantity": 650, "orders": 4}],
        "sell": [{"price": 24611.0, "quantity": 325, "orders": 2}],
    },
}


# ---- the shared row parser ----


def test_a_row_becomes_a_quote_with_its_depth_and_best_prices():
    quote = quote_from_row("INFY", INFY_ROW, broker="zerodha")

    assert quote.symbol == "INFY" and quote.last_price == 1503.25
    assert quote.bid == 1503.0 and quote.ask == 1503.3
    assert quote.depth_bids == (DepthLevel(1503.0, 120), DepthLevel(1502.95, 300))
    assert quote.depth_asks == (DepthLevel(1503.3, 80), DepthLevel(1503.35, 500))


def test_empty_padding_levels_are_dropped_not_kept_as_a_zero_price():
    """Outside the session both brokers send five levels of price 0 / quantity 0."""
    row = {"last_price": 10.0, "depth": {"buy": [{"price": 0, "quantity": 0}], "sell": []}}

    quote = quote_from_row("X", row, broker="zerodha")

    assert quote.depth_bids == () and quote.depth_asks == ()
    assert quote.bid is None and quote.ask is None


def test_open_interest_is_kept_for_a_derivative_and_absent_otherwise():
    assert quote_from_row("NIFTY25OCTFUT", NIFTY_FUT_ROW, broker="zerodha").open_interest == (
        11250300.0
    )
    assert quote_from_row("INFY", INFY_ROW, broker="zerodha").open_interest is None  # oi was 0


@pytest.mark.parametrize("row", [{}, {"last_price": 0}, {"last_price": None, "depth": {}}])
def test_a_row_with_no_price_is_unavailable_not_a_zero_quote(row):
    with pytest.raises(BrokerQuoteUnavailableError):
        quote_from_row("INFY", row, broker="zerodha")


def test_a_row_without_a_depth_still_quotes_the_price():
    quote = quote_from_row("INFY", {"last_price": 1500.0}, broker="upstox")

    assert quote.last_price == 1500.0 and quote.depth_bids == () and quote.bid is None


# ---- Zerodha ----


def _zerodha(handler) -> ZerodhaKiteAdapter:
    return ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(handler))


async def test_zerodha_asks_for_all_symbols_in_one_request():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"NSE:INFY": INFY_ROW, "NFO:NIFTY25OCTFUT": NIFTY_FUT_ROW}}
        )

    quotes = await _zerodha(handler).get_quotes(["NSE:INFY", "NFO:NIFTY25OCTFUT"])

    assert len(requests) == 1
    assert requests[0].url.params.get_list("i") == ["NSE:INFY", "NFO:NIFTY25OCTFUT"]
    assert quotes["NSE:INFY"].depth_asks[0].quantity == 80
    assert quotes["NFO:NIFTY25OCTFUT"].open_interest == 11250300.0


async def test_zerodha_maps_a_bare_symbol_to_nse_and_an_index_to_its_index_key():
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get_list("i"))
        return httpx.Response(
            200,
            json={"data": {"NSE:INFY": INFY_ROW, "NSE:NIFTY 50": {"last_price": 24500.0}}},
        )

    quotes = await _zerodha(handler).get_quotes(["INFY", "NIFTY"])

    assert seen == [["NSE:INFY", "NSE:NIFTY 50"]]
    assert set(quotes) == {"INFY", "NIFTY"}  # keyed by the symbol that was asked about


async def test_zerodha_leaves_out_a_symbol_it_has_no_price_for():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"NSE:INFY": INFY_ROW, "NSE:DEAD": {"last_price": 0}}}
        )

    quotes = await _zerodha(handler).get_quotes(["INFY", "DEAD", "MISSING"])

    assert list(quotes) == ["INFY"]


async def test_zerodha_splits_a_long_list_into_requests_of_a_bounded_size():
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = request.url.params.get_list("i")
        sizes.append(len(keys))
        return httpx.Response(200, json={"data": {k: {"last_price": 10.0} for k in keys}})

    symbols = [f"S{i}" for i in range(QUOTE_BATCH_SIZE * 2 + 5)]
    quotes = await _zerodha(handler).get_quotes(symbols)

    assert sizes == [QUOTE_BATCH_SIZE, QUOTE_BATCH_SIZE, 5]
    assert len(quotes) == len(symbols)


async def test_zerodha_asks_once_about_a_symbol_listed_twice():
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.url.params.get_list("i")))
        return httpx.Response(200, json={"data": {"NSE:INFY": INFY_ROW}})

    await _zerodha(handler).get_quotes(["INFY", "INFY"])

    assert sizes == [1]


async def test_zerodha_single_quote_carries_the_same_depth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"NSE:INFY": INFY_ROW}})

    quote = await _zerodha(handler).get_quote("INFY")

    assert quote.depth_bids[1] == DepthLevel(1502.95, 300)


# ---- Upstox ----


class _Directory:
    """Symbol -> instrument_key without downloading Upstox's instrument file."""

    KEYS = {
        "INFY": "NSE_EQ|INE009A01021",
        "TCS": "NSE_EQ|INE467B01029",
        "NIFTY25OCTFUT": "NSE_FO|57001",
    }

    async def resolve(self, symbol: str) -> str:
        try:
            return self.KEYS[symbol]
        except KeyError:
            raise UpstoxInstrumentNotFoundError(symbol) from None


def _upstox(handler) -> UpstoxAdapter:
    return UpstoxAdapter(
        _CREDENTIALS, transport=httpx.MockTransport(handler), instruments=_Directory()
    )


async def test_upstox_asks_for_all_keys_in_one_request_and_matches_rows_by_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # The key the response object uses is not documented: rows are matched on the token
        # they carry, so these dictionary keys are deliberately unrelated to the request.
        return httpx.Response(
            200,
            json={
                "data": {
                    "A": {**INFY_ROW, "instrument_token": "NSE_EQ|INE009A01021"},
                    "B": {**NIFTY_FUT_ROW, "instrument_token": "NSE_FO|57001"},
                }
            },
        )

    quotes = await _upstox(handler).get_quotes(["INFY", "NIFTY25OCTFUT"])

    assert len(requests) == 1
    assert requests[0].url.params["instrument_key"] == "NSE_EQ|INE009A01021,NSE_FO|57001"
    assert quotes["INFY"].last_price == 1503.25
    assert quotes["NIFTY25OCTFUT"].open_interest == 11250300.0


async def test_upstox_skips_a_symbol_it_does_not_list_and_never_asks_about_it():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["instrument_key"])
        return httpx.Response(
            200,
            json={"data": {"NSE_EQ:INFY": {**INFY_ROW, "instrument_token": "NSE_EQ|INE009A01021"}}},
        )

    quotes = await _upstox(handler).get_quotes(["INFY", "NOSUCH"])

    assert list(quotes) == ["INFY"]
    assert requested == ["NSE_EQ|INE009A01021"]


async def test_upstox_makes_no_request_when_it_lists_none_of_the_symbols():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await _upstox(handler).get_quotes(["NOSUCH", "NEITHER"]) == {}


async def test_upstox_leaves_out_a_symbol_whose_row_has_no_price():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "NSE_EQ:INFY": {**INFY_ROW, "instrument_token": "NSE_EQ|INE009A01021"},
                    "NSE_EQ:TCS": {"last_price": 0, "instrument_token": "NSE_EQ|INE467B01029"},
                }
            },
        )

    quotes = await _upstox(handler).get_quotes(["INFY", "TCS"])

    assert list(quotes) == ["INFY"]


async def test_upstox_splits_a_long_list_into_requests_of_a_bounded_size():
    class _Many:
        async def resolve(self, symbol: str) -> str:
            return f"NSE_EQ|{symbol}"

    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = request.url.params["instrument_key"].split(",")
        sizes.append(len(keys))
        return httpx.Response(
            200,
            json={
                "data": {
                    k.replace("|", ":"): {"last_price": 5.0, "instrument_token": k} for k in keys
                }
            },
        )

    adapter = UpstoxAdapter(
        _CREDENTIALS, transport=httpx.MockTransport(handler), instruments=_Many()
    )
    symbols = [f"S{i}" for i in range(QUOTE_BATCH_SIZE + 1)]

    quotes = await adapter.get_quotes(symbols)

    assert sizes == [QUOTE_BATCH_SIZE, 1] and len(quotes) == len(symbols)


@pytest.mark.parametrize("level", [{"price": 100.0, "quantity": 0}, {"price": 0, "quantity": 50}])
def test_a_level_missing_either_its_price_or_its_quantity_is_not_a_level(level):
    row = {
        "last_price": 100.0,
        "depth": {"buy": [level, {"price": 99.9, "quantity": 10}], "sell": []},
    }

    quote = quote_from_row("X", row, broker="zerodha")

    assert quote.depth_bids == (DepthLevel(99.9, 10),)


def test_a_request_stays_well_under_both_brokers_documented_cap_of_500():
    assert 0 < QUOTE_BATCH_SIZE <= 200


async def test_zerodha_batch_tolerates_a_row_keyed_by_the_bare_symbol():
    """As the single quote does: if the response is keyed by the symbol rather than
    `exchange:symbol`, the row is still found."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"INFY": INFY_ROW}})

    quotes = await _zerodha(handler).get_quotes(["INFY"])

    assert quotes["INFY"].last_price == 1503.25


# ---- the exchange's circuit limits ----


def test_a_quote_carries_the_exchanges_circuit_limits_when_the_broker_sends_them():
    for broker in ("zerodha", "upstox"):
        quote = quote_from_row(
            "INFY",
            {"last_price": 1500.0, "lower_circuit_limit": 1350.0, "upper_circuit_limit": 1650.0},
            broker=broker,
        )

        assert (quote.lower_circuit, quote.upper_circuit) == (1350.0, 1650.0)


def test_limits_sent_as_text_are_read_as_numbers():
    quote = quote_from_row(
        "INFY",
        {"last_price": 1500.0, "lower_circuit_limit": "1350.05", "upper_circuit_limit": "1650.5"},
        broker="zerodha",
    )

    assert (quote.lower_circuit, quote.upper_circuit) == (1350.05, 1650.5)


def test_a_quote_without_limits_has_none_never_a_guess():
    quote = quote_from_row("INFY", {"last_price": 1500.0}, broker="zerodha")

    assert (quote.lower_circuit, quote.upper_circuit) == (None, None)


def test_a_band_is_only_a_band_when_both_ends_are_present_positive_and_in_order():
    def limits(**fields):
        quote = quote_from_row("INFY", {"last_price": 1500.0, **fields}, broker="zerodha")
        return quote.lower_circuit, quote.upper_circuit

    assert limits(lower_circuit_limit=1350.0) == (None, None)  # one end only
    assert limits(upper_circuit_limit=1650.0) == (None, None)
    assert limits(lower_circuit_limit=0, upper_circuit_limit=0) == (None, None)  # no band
    assert limits(lower_circuit_limit=0, upper_circuit_limit=1650.0) == (None, None)
    assert limits(lower_circuit_limit=-5.0, upper_circuit_limit=1650.0) == (None, None)
    assert limits(lower_circuit_limit=1650.0, upper_circuit_limit=1350.0) == (
        None,
        None,
    )  # inverted
    assert limits(lower_circuit_limit=1500.0, upper_circuit_limit=1500.0) == (None, None)  # empty
    assert limits(lower_circuit_limit=None, upper_circuit_limit=None) == (None, None)


def test_limits_that_are_not_numbers_are_ignored_rather_than_breaking_the_quote():
    quote = quote_from_row(
        "INFY",
        {"last_price": 1500.0, "lower_circuit_limit": "n/a", "upper_circuit_limit": "1650"},
        broker="zerodha",
    )

    assert quote.last_price == 1500.0
    assert (quote.lower_circuit, quote.upper_circuit) == (None, None)
