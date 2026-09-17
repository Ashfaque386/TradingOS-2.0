"""Replay-guard + rate-limit tests (Build Spec §18's "replay-guarded,
rate-limited"). Redis-backed, same real-Redis-in-this-sandbox posture as
every other Redis-touching test in this codebase.
"""

import pytest

from src.notifications.replay_guard import check_and_record_replay, check_rate_limit


@pytest.fixture(autouse=True)
async def _clear_notify_keys(redis_client):
    """This file's fixed literal message/sender ids would otherwise
    collide with leftover `notify:*` keys from an earlier run of this
    same file against the real, un-flushed Redis instance."""
    keys = await redis_client.keys("notify:*")
    if keys:
        await redis_client.delete(*keys)


async def test_first_message_id_is_not_a_replay(redis_client):
    result = await check_and_record_replay(redis_client, channel="telegram", message_id="msg-1")
    assert result is True


async def test_same_message_id_seen_twice_is_a_replay_the_second_time(redis_client):
    first = await check_and_record_replay(redis_client, channel="telegram", message_id="msg-2")
    second = await check_and_record_replay(redis_client, channel="telegram", message_id="msg-2")
    assert first is True
    assert second is False


async def test_same_message_id_on_different_channels_is_not_a_replay(redis_client):
    telegram_result = await check_and_record_replay(
        redis_client, channel="telegram", message_id="shared-id"
    )
    discord_result = await check_and_record_replay(
        redis_client, channel="discord", message_id="shared-id"
    )
    assert telegram_result is True
    assert discord_result is True


async def test_rate_limit_allows_up_to_the_limit(redis_client):
    results = [
        await check_rate_limit(redis_client, channel="slack", sender_id="user-a", limit=3)
        for _ in range(3)
    ]
    assert all(results)


async def test_rate_limit_denies_once_exceeded(redis_client):
    for _ in range(3):
        await check_rate_limit(redis_client, channel="slack", sender_id="user-b", limit=3)
    denied = await check_rate_limit(redis_client, channel="slack", sender_id="user-b", limit=3)
    assert denied is False


async def test_rate_limit_is_independent_per_sender(redis_client):
    for _ in range(3):
        await check_rate_limit(redis_client, channel="slack", sender_id="user-c", limit=3)
    other_sender_result = await check_rate_limit(
        redis_client, channel="slack", sender_id="user-d", limit=3
    )
    assert other_sender_result is True
