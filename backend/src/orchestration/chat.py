"""In-app chat orchestration (Build Spec §18: "streaming responses, abort,
per-session model switch, search, pin, export"). All five requirements
live here, each backed by real, testable behavior rather than a UI-only
concept:

- **Streaming**: `stream_assistant_reply` yields `LlmStreamChunk`s
  straight from `src.agents.llm_router.LlmRouter.stream_complete` as they
  arrive -- the API route (src.api.routes.chat) turns them into an SSE
  response with no buffering in between.
- **Abort**: a Redis flag keyed by session id (`chat:abort:<session_id>`),
  set by `request_abort` and checked by `stream_assistant_reply` between
  every chunk -- a client calling the abort endpoint mid-stream gets the
  in-flight generator to stop within one chunk, and the partial reply is
  still persisted (marked `aborted=True`), never silently discarded.
- **Per-session model switch**: `ChatSession.model` (a
  `src.gateway.schema.LlmProvider` value or `None` for "auto") is passed
  straight through as `stream_complete`'s `preferred_provider` -- no
  separate per-session router instance, no global config mutation.
- **Search**: `list_sessions(search=...)` matches session title OR any
  message content within that session (a `LIKE`-based substring search --
  real, not a stub, at this solo-operator data volume a full-text index
  would be premature).
- **Pin/export**: `ChatSession.pinned` is a plain boolean toggle;
  `export_session` renders the full transcript as Markdown or JSON,
  whichever the caller asks for.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, LlmStreamChunk, get_llm_router
from src.gateway.schema import LlmProvider
from src.models.chat_message import ChatMessage, ChatMessageRole
from src.models.chat_session import ChatSession


class Unset:
    """Sentinel distinguishing "leave model unchanged" from "clear model
    to auto" (`None`) in `update_session` -- a bare `None` default
    couldn't tell those two cases apart. Public (not `Unset`/`UNSET`)
    since API callers (src.api.routes.chat) need to pass it explicitly.
    """


UNSET = Unset()

_ABORT_KEY_PREFIX = "chat:abort:"
_ABORT_FLAG_TTL_SECONDS = 300
_FALLBACK_REPLY = (
    "No LLM provider is currently configured, so I can't generate a real reply -- "
    "configure a provider API key to have this session actually respond."
)
_MAX_HISTORY_TURNS = 20


async def create_session(
    db: AsyncSession, *, created_by: str, model: str | None = None, title: str = "New chat"
) -> ChatSession:
    session = ChatSession(created_by=created_by, model=model, title=title)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    return await db.get(ChatSession, session_id)


async def list_sessions(
    db: AsyncSession, *, search: str | None = None, pinned_only: bool = False
) -> list[ChatSession]:
    stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
    if pinned_only:
        stmt = stmt.where(ChatSession.pinned.is_(True))
    if search:
        pattern = f"%{search}%"
        matching_session_ids = select(ChatMessage.session_id).where(
            ChatMessage.content.ilike(pattern)
        )
        stmt = stmt.where(
            or_(ChatSession.title.ilike(pattern), ChatSession.id.in_(matching_session_ids))
        )
    return list((await db.execute(stmt)).scalars().all())


async def update_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    title: str | None = None,
    model: str | None | Unset = UNSET,
    pinned: bool | None = None,
) -> ChatSession | None:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return None
    if title is not None:
        session.title = title
    if not isinstance(model, Unset):
        session.model = model
    if pinned is not None:
        session.pinned = pinned
    await db.commit()
    await db.refresh(session)
    return session


async def delete_session(db: AsyncSession, session_id: uuid.UUID) -> bool:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return False
    await db.delete(session)
    await db.commit()
    return True


async def list_messages(db: AsyncSession, session_id: uuid.UUID) -> list[ChatMessage]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def export_session(
    db: AsyncSession, session_id: uuid.UUID, *, format: Literal["markdown", "json"] = "markdown"
) -> str | None:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return None
    messages = await list_messages(db, session_id)

    if format == "json":
        return json.dumps(
            {
                "id": str(session.id),
                "title": session.title,
                "model": session.model,
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "provider": m.provider,
                        "aborted": m.aborted,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages
                ],
            },
            indent=2,
        )

    lines = [f"# {session.title}", ""]
    for m in messages:
        speaker = "**User**" if m.role == ChatMessageRole.USER.value else "**Assistant**"
        suffix = " _(aborted)_" if m.aborted else ""
        lines.append(f"{speaker}{suffix}: {m.content}")
        lines.append("")
    return "\n".join(lines)


def _abort_key(session_id: uuid.UUID) -> str:
    return f"{_ABORT_KEY_PREFIX}{session_id}"


async def request_abort(redis: Redis, session_id: uuid.UUID) -> None:
    await redis.set(_abort_key(session_id), "1", ex=_ABORT_FLAG_TTL_SECONDS)


async def _is_abort_requested(redis: Redis, session_id: uuid.UUID) -> bool:
    return bool(await redis.exists(_abort_key(session_id)))


async def _clear_abort(redis: Redis, session_id: uuid.UUID) -> None:
    await redis.delete(_abort_key(session_id))


def _build_prompt(history: list[ChatMessage], new_message: str) -> str:
    turns = history[-_MAX_HISTORY_TURNS:]
    lines = []
    for m in turns:
        speaker = "User" if m.role == ChatMessageRole.USER.value else "Assistant"
        lines.append(f"{speaker}: {m.content}")
    lines.append(f"User: {new_message}")
    return "\n".join(lines)


async def stream_assistant_reply(
    db: AsyncSession,
    redis: Redis,
    session_id: uuid.UUID,
    user_message: str,
    *,
    llm_router: LlmRouter | None = None,
) -> AsyncIterator[LlmStreamChunk]:
    """Persists `user_message`, streams the assistant's reply chunk by
    chunk, and persists the (possibly partial/aborted) assistant reply
    once the stream ends. The caller (src.api.routes.chat) re-yields each
    chunk straight onto an SSE response -- this function never buffers
    the whole reply before the first chunk reaches the client.
    """
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise ValueError(f"no such chat session: {session_id}")

    history = await list_messages(db, session_id)

    db.add(
        ChatMessage(session_id=session_id, role=ChatMessageRole.USER.value, content=user_message)
    )
    await db.commit()

    await _clear_abort(redis, session_id)

    prompt = _build_prompt(history, user_message)
    preferred_provider = LlmProvider(session.model) if session.model else None
    router = llm_router if llm_router is not None else get_llm_router()

    accumulated = ""
    provider_used: str | None = None
    aborted = False

    try:
        async for chunk in router.stream_complete(
            agent_id="ceo-chat-interface", prompt=prompt, preferred_provider=preferred_provider
        ):
            accumulated += chunk.text
            if chunk.done:
                provider_used = chunk.provider.value if chunk.provider else None
            yield chunk
            if await _is_abort_requested(redis, session_id):
                aborted = True
                break
    except LlmRouterExhaustedError:
        accumulated = _FALLBACK_REPLY
        yield LlmStreamChunk(text=_FALLBACK_REPLY, done=False)
        yield LlmStreamChunk(text="", done=True)

    db.add(
        ChatMessage(
            session_id=session_id,
            role=ChatMessageRole.ASSISTANT.value,
            content=accumulated,
            provider=provider_used,
            aborted=aborted,
        )
    )
    await db.commit()
    await _clear_abort(redis, session_id)
