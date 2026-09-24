"""Shared fixed-window rate-limit counter, backed by Redis (the same
shared resource `src.core.redis_client` already provides everywhere else
in this codebase). Extracted from `src.notifications.replay_guard`
(Build Spec §18's inbound-webhook limiter, the original caller) so the
general API-wide limiter (`src.observability.rate_limit_middleware`,
Phase 21) uses the exact same primitive rather than a second
hand-rolled counter.

A plain fixed-window counter (`INCR` + `EXPIRE` on the window's first
increment), not a token bucket -- this codebase's traffic at solo-operator
scale doesn't need smoothing, only a hard ceiling per window, the same
reasoning `replay_guard`'s own original docstring gave for its narrower
webhook-only case.
"""

import time

from redis.asyncio import Redis


async def fixed_window_increment(
    redis: Redis, *, key: str, limit: int, window_seconds: int
) -> bool:
    """Returns True if `key` is still within `limit` for the current fixed
    window, False if this call would exceed it. `key` should already
    encode whatever the caller wants windowed on (channel+sender, IP,
    user id, ...) -- this function only adds the window-bucket suffix."""
    window_bucket = int(time.time()) // window_seconds
    bucket_key = f"{key}:{window_bucket}"
    count = await redis.incr(bucket_key)
    if count == 1:
        await redis.expire(bucket_key, window_seconds)
    return count <= limit
