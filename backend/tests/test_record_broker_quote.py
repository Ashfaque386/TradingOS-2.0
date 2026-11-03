"""The recorder that captures Upstox's quote answer (`scripts/record_broker_quote.py`).

What matters is that the recording is the broker's answer untouched, says when it was made and
whether the session was open, and carries nothing secret. Upstox's exchange stamp stays unread
(`_exchange_time`) until a real recording settles which field it is; that is also pinned here, so
the day it is wired up the test that says "None until then" has to be changed on purpose.
"""

import json
from datetime import UTC, datetime

import httpx

from scripts.record_broker_quote import ENDPOINT, record_quotes
from src.brokers.base import BrokerCredentials, quote_from_row
from src.brokers.upstox import UpstoxAdapter
from tests.test_brokers_upstox import INFY_KEY, _StubInstruments

SECRET = "tok-must-never-be-recorded"
ANSWER = {
    "status": "success",
    "data": {
        "NSE_EQ:INFY": {
            "instrument_token": INFY_KEY,
            "last_price": 1161.2,
            "timestamp": "2026-11-03T10:15:00.000+05:30",
            "last_trade_time": "1793695500000",
        }
    },
}


def _adapter(handler) -> UpstoxAdapter:
    return UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token=SECRET),
        transport=httpx.MockTransport(handler),
        instruments=_StubInstruments(),
    )


async def test_the_recording_holds_the_answer_untouched_and_when_and_whether_the_market_was_open():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=ANSWER)

    open_session = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)  # 10:30 IST, a trading day
    recording = await record_quotes(_adapter(handler), ["INFY"], now=open_session)

    assert recording["quotes"]["INFY"] == {"instrument_key": INFY_KEY, "response": ANSWER}
    assert recording["endpoint"] == ENDPOINT and recording["broker"] == "upstox"
    assert recording["captured_at_utc"] == "2026-11-03T05:00:00+00:00"
    assert recording["nse_session_open"] is True
    assert asked == [("/v2/market-quote/quotes", {"instrument_key": INFY_KEY})]


async def test_a_recording_made_after_the_close_says_the_session_was_closed():
    handler = lambda request: httpx.Response(200, json=ANSWER)  # noqa: E731
    closed = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # 17:30 IST

    recording = await record_quotes(_adapter(handler), ["INFY"], now=closed)

    assert recording["nse_session_open"] is False


async def test_nothing_secret_reaches_the_recording():
    handler = lambda request: httpx.Response(200, json=ANSWER)  # noqa: E731

    recording = await record_quotes(_adapter(handler), ["INFY"], now=datetime.now(UTC))

    assert SECRET not in json.dumps(recording)


def test_upstox_has_no_exchange_stamp_until_a_recording_settles_the_field():
    row = ANSWER["data"]["NSE_EQ:INFY"]

    assert quote_from_row("INFY", row, broker="upstox").exchange_time is None
