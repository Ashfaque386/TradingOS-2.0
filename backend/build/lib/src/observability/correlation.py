"""Correlation ID propagation (Build Spec §19): "structured logging via
structlog, with a correlation ID threaded from HTTP request -> orchestration
run -> agent action -> order." Implemented as a pure ASGI middleware
(not Starlette's `BaseHTTPMiddleware`, which has a history of context-
propagation quirks across the task boundary `call_next` introduces) so a
correlation ID bound via `structlog.contextvars.bind_contextvars` before
the downstream app runs is guaranteed visible to every `structlog` log
call made anywhere in that same request's call chain -- no code at any
inner layer needs to know this middleware exists or pass the ID along
manually. That's what actually satisfies "threaded through orchestration
-> agent -> order": Phase 2's orchestration run creation, Phase 3's
LangGraph node execution, and Phase 8/9's order placement all run
synchronously inside the same request when triggered via their HTTP
routes, so they already share this same contextvar automatically.

A request may arrive with its own `X-Correlation-ID` (a caller tracing
its own multi-service flow); this middleware honors it instead of
generating a fresh one, and always echoes the final value back on the
response header either way.

**Autonomous work has no HTTP request to inherit from.** Every APScheduler
job across this codebase (Phase 2's stall sweep, Phase 3's heartbeat,
Phase 7/9/10/11's scheduled pipelines) binds its own fresh correlation ID
at the top of each invocation instead -- see each scheduler module for
the one-line `structlog.contextvars.bound_contextvars(...)` wrapping its
job body. A correlation ID is really "one traceable unit of work," and a
scheduled job tick is exactly that, just not one that started with an
HTTP request.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import ParamSpec, TypeVar

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

CORRELATION_ID_HEADER = "X-Correlation-ID"

_P = ParamSpec("_P")
_R = TypeVar("_R")


@asynccontextmanager
async def bind_job_correlation_id(job_name: str) -> AsyncIterator[str]:
    """For autonomous work with no originating HTTP request (every
    APScheduler/asyncio-loop job across this codebase) -- binds a fresh,
    per-invocation correlation ID so every log line inside one job tick
    shares a traceable ID, the same way one HTTP request's log lines do
    via `CorrelationIdMiddleware`. See that middleware's docstring for why
    a scheduled job tick needs its own ID rather than inheriting one.
    """
    correlation_id = f"job:{job_name}:{uuid.uuid4().hex}"
    tokens = structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    try:
        yield correlation_id
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def with_job_correlation_id(
    job_name: str, fn: Callable[_P, Awaitable[_R]]
) -> Callable[_P, Awaitable[_R]]:
    """Wraps an APScheduler job callable so every invocation gets its own
    bound correlation ID with no change to the job function's own body --
    used at `add_job(...)` registration time, once per job, rather than
    editing every job function individually.
    """

    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        async with bind_job_correlation_id(job_name):
            return await fn(*args, **kwargs)

    wrapped.__name__ = getattr(fn, "__name__", job_name)
    return wrapped


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(CORRELATION_ID_HEADER) or uuid.uuid4().hex
        scope.setdefault("state", {})
        scope["state"]["correlation_id"] = correlation_id

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.append(CORRELATION_ID_HEADER, correlation_id)
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
