"""Shared async Redis client, same lazily-constructed-singleton pattern as
src.core.db's engine. Used by src.orchestration.events for the event bus's
publish side (Build Spec §7.3, §5.4).
"""

from functools import lru_cache

from redis.asyncio import Redis

from src.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)
