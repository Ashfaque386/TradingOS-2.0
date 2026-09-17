"""Replay guard + rate limiting for inbound webhooks (Build Spec §18),
backed by Redis -- the same shared resource Phase 2's event bus and
Phase 7's tick streams already use, reused here rather than adding a
second in-memory or DB-backed mechanism. Both checks are per-process-
independent (multiple API workers share one Redis), which an in-memory
dict could never be.

**Replay guard**: `SET key value NX EX ttl` is atomic -- it can only
succeed once per key within the TTL window, so two concurrent requests
for the same (channel, message_id) can never both be treated as "new".
Signature verification alone (src.notifications.verification) proves a
request was genuinely sent by the channel at some point; it does *not*
prove this exact request hasn't already been processed -- an attacker (or
a channel's own at-least-once delivery retry) replaying a previously
valid, correctly-signed payload would sail through signature checks
alone. This is what actually makes a request "replay-guarded", not just
"signed".

**Rate limit**: a plain fixed-window counter (`INCR` + `EXPIRE` on the
window's first increment) -- not a token bucket, since inbound webhook
traffic at this solo-operator scale doesn't need smoothing, only a hard
ceiling per window.
"""

import time

from redis.asyncio import Redis

DEFAULT_REPLAY_TTL_SECONDS = 600
DEFAULT_RATE_LIMIT = 20
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60


async def check_and_record_replay(
    redis: Redis,
    *,
    channel: str,
    message_id: str,
    ttl_seconds: int = DEFAULT_REPLAY_TTL_SECONDS,
) -> bool:
    """Returns True the first time this (channel, message_id) is seen
    within the TTL window, False on every subsequent (replayed) call."""
    key = f"notify:seen:{channel}:{message_id}"
    was_new = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    return bool(was_new)


async def check_rate_limit(
    redis: Redis,
    *,
    channel: str,
    sender_id: str,
    limit: int = DEFAULT_RATE_LIMIT,
    window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
) -> bool:
    """Returns True if this sender is still within the rate limit for the
    current fixed window, False if this call would exceed it."""
    window_bucket = int(time.time()) // window_seconds
    key = f"notify:ratelimit:{channel}:{sender_id}:{window_bucket}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count <= limit
