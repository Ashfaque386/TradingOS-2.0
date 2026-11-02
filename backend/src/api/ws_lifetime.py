"""How long a WebSocket handler lives: until its work ends or the browser goes away.

A handler that only writes to the socket when it has something to say (a tick, an audit row, a
pub/sub message) never reads from it, so it never learns that the browser has gone: it waits for the
next message, which may be hours off when the market is shut, and holds its Redis connection and its
task the whole time. `serve_until_client_leaves` reads the socket alongside the work and stops the
work the moment the browser disconnects.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from fastapi import WebSocket


async def _client_gone(websocket: WebSocket) -> None:
    """Returns when the browser disconnects. Whatever it sends meanwhile is not used: these channels
    only carry data out."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def serve_until_client_leaves(websocket: WebSocket, work: Coroutine[Any, Any, None]) -> None:
    """Runs `work` until it finishes or the browser disconnects, whichever comes first; the other is
    cancelled. What `work` raises (a failed send is a `WebSocketDisconnect`) is raised from here."""
    serving = asyncio.ensure_future(work)
    watching = asyncio.ensure_future(_client_gone(websocket))
    try:
        await asyncio.wait({serving, watching}, return_when=asyncio.FIRST_COMPLETED)
        if serving.done():
            serving.result()
    finally:
        for task in (serving, watching):
            if not task.done():
                task.cancel()
        await asyncio.gather(serving, watching, return_exceptions=True)
