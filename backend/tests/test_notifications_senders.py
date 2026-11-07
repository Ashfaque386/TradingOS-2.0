"""What a failed outbound send reports. The person reading Settings or a log has to be told what
failed, and most of httpx's transport errors (a timeout, a refused connection) carry no message of
their own: the first version reported `error: ""` for a Telegram alert that could not be delivered.
"""

import httpx
import pytest

from src.notifications.channel_store import NotificationChannelConfig
from src.notifications.senders import describe_error, send_discord, send_slack, send_telegram

TELEGRAM = NotificationChannelConfig(bot_token="123:SECRET-TOKEN", chat_id="42")
WEBHOOK = NotificationChannelConfig(webhook_url="https://hooks.example.test/abc")

SENDERS = [
    pytest.param(send_telegram, TELEGRAM, id="telegram"),
    pytest.param(send_discord, WEBHOOK, id="discord"),
    pytest.param(send_slack, WEBHOOK, id="slack"),
]


def _raising(exc: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def test_an_error_with_no_message_is_named_by_its_type():
    assert describe_error(TimeoutError()) == "TimeoutError"
    assert describe_error(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_an_error_with_a_message_keeps_it():
    assert describe_error(ValueError("bad chat id")) == "ValueError: bad chat id"


@pytest.mark.parametrize(("send", "config"), SENDERS)
async def test_a_timeout_is_reported_by_name_not_as_an_empty_string(send, config):
    result = await send(config, "hello", transport=_raising(httpx.ConnectTimeout("")))

    assert result.ok is False
    assert result.status_code is None
    assert result.error == "ConnectTimeout"


@pytest.mark.parametrize(("send", "config"), SENDERS)
async def test_a_connection_error_keeps_its_message_after_the_type(send, config):
    result = await send(
        config, "hello", transport=_raising(httpx.ConnectError("name resolution failed"))
    )

    assert result.ok is False
    assert result.error == "ConnectError: name resolution failed"


@pytest.mark.parametrize(("send", "config"), SENDERS)
async def test_the_error_never_carries_the_credential(send, config):
    result = await send(config, "hello", transport=_raising(httpx.ReadTimeout("")))

    assert result.error is not None
    assert "SECRET-TOKEN" not in result.error
    assert "hooks.example.test" not in result.error


@pytest.mark.parametrize(("send", "config"), SENDERS)
async def test_a_refusal_by_the_channel_still_reports_its_status_and_reason(send, config):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, text='{"ok":false,"description":"Unauthorized"}')
    )

    result = await send(config, "hello", transport=transport)

    assert result.ok is False
    assert result.status_code == 401
    assert result.error == '{"ok":false,"description":"Unauthorized"}'


@pytest.mark.parametrize(("send", "config"), SENDERS)
async def test_a_delivered_message_has_no_error(send, config):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    result = await send(config, "hello", transport=transport)

    assert result.ok is True
    assert result.error is None
