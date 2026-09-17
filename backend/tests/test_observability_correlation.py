"""CorrelationIdMiddleware tests (Build Spec §19): a correlation ID is
generated (or honored, if the caller supplied one) and echoed back on
every HTTP response, plus the two APScheduler-job helpers that give
autonomous work its own bound ID (src/observability/correlation.py's
module docstring explains why jobs can't inherit one from a request).
"""

import structlog

from src.observability.correlation import (
    CORRELATION_ID_HEADER,
    bind_job_correlation_id,
    with_job_correlation_id,
)


async def test_response_carries_a_generated_correlation_id_header(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert CORRELATION_ID_HEADER in resp.headers
    assert len(resp.headers[CORRELATION_ID_HEADER]) > 0


async def test_response_echoes_a_caller_supplied_correlation_id(client):
    resp = await client.get("/health", headers={CORRELATION_ID_HEADER: "caller-supplied-id-123"})
    assert resp.status_code == 200
    assert resp.headers[CORRELATION_ID_HEADER] == "caller-supplied-id-123"


async def test_two_separate_requests_get_two_different_generated_ids(client):
    resp1 = await client.get("/health")
    resp2 = await client.get("/health")
    assert resp1.headers[CORRELATION_ID_HEADER] != resp2.headers[CORRELATION_ID_HEADER]


async def test_bind_job_correlation_id_binds_and_resets_the_contextvar():
    assert structlog.contextvars.get_contextvars().get("correlation_id") is None

    async with bind_job_correlation_id("my_job") as correlation_id:
        assert correlation_id.startswith("job:my_job:")
        assert structlog.contextvars.get_contextvars()["correlation_id"] == correlation_id

    assert structlog.contextvars.get_contextvars().get("correlation_id") is None


async def test_bind_job_correlation_id_produces_a_fresh_id_per_call():
    async with bind_job_correlation_id("my_job") as id1:
        pass
    async with bind_job_correlation_id("my_job") as id2:
        pass
    assert id1 != id2


async def test_with_job_correlation_id_wraps_a_job_callable_with_a_bound_id():
    seen_ids = []

    async def job_fn(x: int) -> int:
        seen_ids.append(structlog.contextvars.get_contextvars().get("correlation_id"))
        return x * 2

    wrapped = with_job_correlation_id("my_job", job_fn)
    result = await wrapped(21)

    assert result == 42
    assert len(seen_ids) == 1
    assert seen_ids[0] is not None
    assert seen_ids[0].startswith("job:my_job:")
    # The contextvar doesn't leak outside the wrapped call.
    assert structlog.contextvars.get_contextvars().get("correlation_id") is None
