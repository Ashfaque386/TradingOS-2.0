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
