"""Shared async Redis client, same lazily-constructed-singleton pattern as
src.core.db's engine. Used by src.orchestration.events for the event bus's
publish side (Build Spec §7.3, §5.4).
"""

from functools import lru_cache
from typing import cast

from redis.asyncio import Redis

from src.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    # redis-py's own from_url() return type is untyped (Any) regardless of
    # the real Redis instance it constructs.
    return cast(Redis, Redis.from_url(settings.redis_url, decode_responses=True))
