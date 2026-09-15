import asyncio
import uuid

from sqlalchemy import select

from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_event import OrganizationalEvent
from src.orchestration import events


async def _make_run(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def test_emit_assigns_sequential_numbers(db_session_factory, redis_client):
    run_id = await _make_run(db_session_factory)

    async with db_session_factory() as db:
        e1 = await events.emit(db, redis_client, run_id=run_id, event_type="a", payload={})
    async with db_session_factory() as db:
        e2 = await events.emit(db, redis_client, run_id=run_id, event_type="b", payload={})
    async with db_session_factory() as db:
        e3 = await events.emit(db, redis_client, run_id=run_id, event_type="c", payload={})

    assert [e1.sequence, e2.sequence, e3.sequence] == [1, 2, 3]


async def test_emit_sequence_is_gap_free_under_concurrency(db_session_factory, redis_client):
    run_id = await _make_run(db_session_factory)

    async def _emit(i: int):
        async with db_session_factory() as db:
            return await events.emit(
                db, redis_client, run_id=run_id, event_type=f"concurrent-{i}", payload={}
            )

    results = await asyncio.gather(*(_emit(i) for i in range(10)))
    sequences = sorted(e.sequence for e in results)
    assert sequences == list(range(1, 11))  # 1..10, no gaps, no duplicates

    async with db_session_factory() as db:
        result = await db.execute(
            select(OrganizationalEvent.sequence).where(OrganizationalEvent.run_id == run_id)
        )
        stored = sorted(s for (s,) in result.all())
        assert stored == list(range(1, 11))


async def test_emit_sequences_are_independent_per_run(db_session_factory, redis_client):
    run_a = await _make_run(db_session_factory)
    run_b = await _make_run(db_session_factory)

    async with db_session_factory() as db:
        ea1 = await events.emit(db, redis_client, run_id=run_a, event_type="x", payload={})
    async with db_session_factory() as db:
        eb1 = await events.emit(db, redis_client, run_id=run_b, event_type="x", payload={})
    async with db_session_factory() as db:
        ea2 = await events.emit(db, redis_client, run_id=run_a, event_type="y", payload={})

    assert (ea1.sequence, ea2.sequence) == (1, 2)
    assert eb1.sequence == 1


async def test_emit_publishes_to_redis(db_session_factory, redis_client):
    run_id = await _make_run(db_session_factory)
    channel = f"organization-events:{run_id}"

    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        # Drain the subscribe confirmation message.
        await pubsub.get_message(timeout=1)

        async with db_session_factory() as db:
            await events.emit(
                db, redis_client, run_id=run_id, event_type="published", payload={"k": "v"}
            )

        message = await pubsub.get_message(timeout=2)
        assert message is not None
        assert message["type"] == "message"
        assert "published" in message["data"]
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


async def test_emit_survives_redis_publish_failure(db_session_factory):
    """A Redis outage must never block the durable Postgres insert."""
    run_id = await _make_run(db_session_factory)

    class _BrokenRedis:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("redis is down")

    async with db_session_factory() as db:
        event = await events.emit(
            db, _BrokenRedis(), run_id=run_id, event_type="still_recorded", payload={}
        )
    assert event.sequence == 1

    async with db_session_factory() as db:
        result = await db.execute(
            select(OrganizationalEvent).where(OrganizationalEvent.run_id == run_id)
        )
        stored = result.scalar_one()
        assert stored.event_type == "still_recorded"
