"""Dedicated mid-stream abort test (Build Spec §18's "abort") exercising
the real Redis abort-flag check in src.orchestration.chat.stream_assistant_reply
directly -- the ASGI test client used by test_chat_api.py buffers the whole
SSE response body, so it can't practically pause a live request mid-stream
to call the abort endpoint in between chunks. Driving the async generator
by hand, one chunk at a time, is the only way to genuinely exercise the
"abort between chunk N and chunk N+1" code path with a real Redis flag.
"""

from collections.abc import AsyncIterator

from sqlalchemy import select

from src.agents.llm_router import LlmStreamChunk
from src.gateway.schema import LlmProvider
from src.models.chat_message import ChatMessage, ChatMessageRole
from src.orchestration import chat as chat_orchestration


class _MultiChunkRouter:
    """A fake LlmRouter whose stream_complete yields several chunks --
    real code under test never gets to see all of them, since the test
    aborts after the first."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_complete(
        self, *, agent_id: str, prompt: str, model: str = "auto", preferred_provider=None
    ) -> AsyncIterator[LlmStreamChunk]:
        for i, text in enumerate(self._chunks):
            is_last = i == len(self._chunks) - 1
            yield LlmStreamChunk(
                text=text, done=is_last, provider=LlmProvider.ANTHROPIC if is_last else None
            )


async def test_abort_requested_between_chunks_stops_the_stream_and_marks_aborted(
    db_session_factory, redis_client
):
    async with db_session_factory() as db:
        session = await chat_orchestration.create_session(db, created_by="op@example.com")
        session_id = session.id

    fake_router = _MultiChunkRouter(["first chunk ", "second chunk ", "third chunk"])

    received: list[LlmStreamChunk] = []
    async with db_session_factory() as db:
        gen = chat_orchestration.stream_assistant_reply(
            db, redis_client, session_id, "go", llm_router=fake_router
        )

        first_chunk = await gen.__anext__()
        received.append(first_chunk)
        assert first_chunk.text == "first chunk "

        # Abort right after the first chunk reached the "client" -- the
        # generator is paused at that `yield`, about to check the flag.
        await chat_orchestration.request_abort(redis_client, session_id)

        # The generator must stop instead of yielding "second chunk ".
        remaining = [chunk async for chunk in gen]
        assert remaining == []

    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 2
    user_message, assistant_message = rows
    assert user_message.role == ChatMessageRole.USER.value
    assert assistant_message.role == ChatMessageRole.ASSISTANT.value
    assert assistant_message.content == "first chunk "
    assert assistant_message.aborted is True


async def test_no_abort_requested_streams_every_chunk_and_is_not_aborted(
    db_session_factory, redis_client
):
    async with db_session_factory() as db:
        session = await chat_orchestration.create_session(db, created_by="op@example.com")
        session_id = session.id

    fake_router = _MultiChunkRouter(["only chunk"])

    async with db_session_factory() as db:
        chunks = [
            chunk
            async for chunk in chat_orchestration.stream_assistant_reply(
                db, redis_client, session_id, "go", llm_router=fake_router
            )
        ]

    assert len(chunks) == 1
    assert chunks[0].done is True

    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )

    assistant_message = rows[1]
    assert assistant_message.content == "only chunk"
    assert assistant_message.aborted is False
    assert assistant_message.provider == LlmProvider.ANTHROPIC.value
