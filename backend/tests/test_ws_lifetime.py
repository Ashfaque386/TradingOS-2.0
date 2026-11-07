"""A WebSocket handler that has nothing to send must still notice the browser leaving.

The tick, audit, and pub/sub channels write only when there is something new, so before this they
never read the socket: a closed tab left the handler (and the pooled database connection its session
held) alive until the next message, which on a shut market is never.
"""

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from src.api.ws_lifetime import serve_until_client_leaves


class FakeWebSocket:
    """What the helper reads: `receive()`, fed from a queue."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def receive(self) -> dict[str, str]:
        return await self.incoming.get()


class Work:
    """Idle work that records how it ended."""

    def __init__(self) -> None:
        self.cancelled = False
        self.started = asyncio.Event()

    async def idle_forever(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_idle_work_stops_when_the_browser_disconnects():
    websocket, work = FakeWebSocket(), Work()
    serving = asyncio.ensure_future(serve_until_client_leaves(websocket, work.idle_forever()))
    await work.started.wait()

    websocket.incoming.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(serving, timeout=2)

    assert work.cancelled


async def test_what_the_browser_sends_does_not_end_the_work():
    websocket, work = FakeWebSocket(), Work()
    serving = asyncio.ensure_future(serve_until_client_leaves(websocket, work.idle_forever()))
    await work.started.wait()

    websocket.incoming.put_nowait({"type": "websocket.receive", "text": "hello"})
    await asyncio.sleep(0.05)

    assert not serving.done() and not work.cancelled
    websocket.incoming.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(serving, timeout=2)


async def test_work_that_finishes_first_ends_the_wait_and_the_reading():
    websocket = FakeWebSocket()

    async def quick() -> None:
        return None

    await asyncio.wait_for(serve_until_client_leaves(websocket, quick()), timeout=2)

    # nothing is left reading the socket: a message put there now stays in the queue
    websocket.incoming.put_nowait({"type": "websocket.receive"})
    await asyncio.sleep(0.05)
    assert websocket.incoming.qsize() == 1


async def test_what_the_work_raises_is_raised():
    websocket = FakeWebSocket()

    async def send_to_a_closed_socket() -> None:
        raise WebSocketDisconnect(1006)

    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(
            serve_until_client_leaves(websocket, send_to_a_closed_socket()), timeout=2
        )


# What uvicorn raises for a send that races the browser leaving.
CLOSED_SOCKET_SEND = RuntimeError(
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close' "
    "or response already completed."
)


async def test_a_send_that_races_the_browser_leaving_is_the_browser_leaving():
    websocket = FakeWebSocket()
    websocket.incoming.put_nowait({"type": "websocket.disconnect"})

    async def send_as_the_browser_goes() -> None:
        # fails in the same pass of the loop in which the reader sees the disconnect
        raise CLOSED_SOCKET_SEND

    await asyncio.wait_for(
        serve_until_client_leaves(websocket, send_as_the_browser_goes()), timeout=2
    )


async def test_the_reader_may_see_the_browser_go_just_after_the_failed_send(monkeypatch):
    monkeypatch.setattr("src.api.ws_lifetime.CLIENT_LEFT_GRACE_SECONDS", 1.0)
    websocket = FakeWebSocket()

    async def send_to_a_socket_that_just_closed() -> None:
        asyncio.get_running_loop().call_later(
            0.05, websocket.incoming.put_nowait, {"type": "websocket.disconnect"}
        )
        raise CLOSED_SOCKET_SEND

    await asyncio.wait_for(
        serve_until_client_leaves(websocket, send_to_a_socket_that_just_closed()), timeout=2
    )


async def test_the_same_error_with_the_browser_still_there_is_a_fault(monkeypatch):
    monkeypatch.setattr("src.api.ws_lifetime.CLIENT_LEFT_GRACE_SECONDS", 0.05)
    websocket = FakeWebSocket()  # nothing ever arrives: the browser is connected

    async def failing_send() -> None:
        raise CLOSED_SOCKET_SEND

    with pytest.raises(RuntimeError, match="Unexpected ASGI message"):
        await asyncio.wait_for(serve_until_client_leaves(websocket, failing_send()), timeout=2)


async def test_other_errors_are_raised_even_as_the_browser_leaves():
    websocket = FakeWebSocket()
    websocket.incoming.put_nowait({"type": "websocket.disconnect"})

    async def broken() -> None:
        raise ValueError("a real bug")

    with pytest.raises(ValueError, match="a real bug"):
        await asyncio.wait_for(serve_until_client_leaves(websocket, broken()), timeout=2)


async def test_stopping_the_handler_stops_the_work_too():
    websocket, work = FakeWebSocket(), Work()
    serving = asyncio.ensure_future(serve_until_client_leaves(websocket, work.idle_forever()))
    await work.started.wait()

    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert work.cancelled


async def test_every_channel_that_waits_for_data_watches_for_the_browser_leaving():
    """A channel added without it would hold its connection after the tab closed."""
    import inspect

    from src.api.routes import websockets

    source = inspect.getsource(websockets)
    # activity-feed, agent-logs, organization-events, agent-pipeline-events, ticks
    assert source.count("await serve_until_client_leaves(") == 5
