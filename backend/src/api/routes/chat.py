"""In-app chat API (Build Spec §18): streaming replies over Server-Sent
Events, abort, per-session model switch, search, pin, export. Available
to every authenticated role -- chat with the CEO Agent is a general
operator tool, not a sensitive per-role surface, the same posture this
codebase already takes for strategies/backtests (shared organizational
state, not per-user-private data).
"""

import json
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    ChatMessageResponse,
    ChatSessionResponse,
    CreateChatSessionRequest,
    SendChatMessageRequest,
    UpdateChatSessionRequest,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.models.user import User
from src.orchestration import chat as chat_orchestration

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat/sessions", tags=["chat"])

register_policy("POST", "/api/v1/chat/sessions", roles=list(Role))
register_policy("GET", "/api/v1/chat/sessions", roles=list(Role))
register_policy("GET", "/api/v1/chat/sessions/{session_id}", roles=list(Role))
register_policy("PATCH", "/api/v1/chat/sessions/{session_id}", roles=list(Role))
register_policy("DELETE", "/api/v1/chat/sessions/{session_id}", roles=list(Role))
register_policy("GET", "/api/v1/chat/sessions/{session_id}/messages", roles=list(Role))
register_policy("POST", "/api/v1/chat/sessions/{session_id}/messages", roles=list(Role))
register_policy("POST", "/api/v1/chat/sessions/{session_id}/abort", roles=list(Role))
register_policy("GET", "/api/v1/chat/sessions/{session_id}/export", roles=list(Role))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session_endpoint(
    body: CreateChatSessionRequest,
    current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionResponse:
    session = await chat_orchestration.create_session(
        db, created_by=current_user.email, model=body.model, title=body.title
    )
    return ChatSessionResponse.model_validate(session)


@router.get("")
async def list_sessions_endpoint(
    search: str | None = None,
    pinned_only: bool = False,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSessionResponse]:
    sessions = await chat_orchestration.list_sessions(db, search=search, pinned_only=pinned_only)
    return [ChatSessionResponse.model_validate(s) for s in sessions]


@router.get("/{session_id}")
async def get_session_endpoint(
    session_id: uuid.UUID,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionResponse:
    session = await chat_orchestration.get_session(db, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    return ChatSessionResponse.model_validate(session)


@router.get("/{session_id}/messages")
async def list_messages_endpoint(
    session_id: uuid.UUID,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessageResponse]:
    if await chat_orchestration.get_session(db, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    messages = await chat_orchestration.list_messages(db, session_id)
    return [ChatMessageResponse.model_validate(m) for m in messages]


@router.patch("/{session_id}")
async def update_session_endpoint(
    session_id: uuid.UUID,
    body: UpdateChatSessionRequest,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionResponse:
    model_update = (
        None
        if body.clear_model
        else (body.model if body.model is not None else chat_orchestration.UNSET)
    )
    session = await chat_orchestration.update_session(
        db, session_id, title=body.title, model=model_update, pinned=body.pinned
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    return ChatSessionResponse.model_validate(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session_endpoint(
    session_id: uuid.UUID,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await chat_orchestration.delete_session(db, session_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")


@router.post("/{session_id}/messages")
async def send_message_endpoint(
    session_id: uuid.UUID,
    body: SendChatMessageRequest,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    if await chat_orchestration.get_session(db, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")

    async def _event_stream():
        async for chunk in chat_orchestration.stream_assistant_reply(
            db, redis, session_id, body.content
        ):
            payload = {
                "text": chunk.text,
                "done": chunk.done,
                "provider": chunk.provider.value if chunk.provider else None,
            }
            yield f"data: {json.dumps(payload)}\n\n"

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.post("/{session_id}/abort", status_code=status.HTTP_204_NO_CONTENT)
async def abort_message_endpoint(
    session_id: uuid.UUID,
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    if await chat_orchestration.get_session(db, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    await chat_orchestration.request_abort(redis, session_id)


@router.get("/{session_id}/export")
async def export_session_endpoint(
    session_id: uuid.UUID,
    format: str = "markdown",
    _current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if format not in ("markdown", "json"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "format must be 'markdown' or 'json'")
    content = await chat_orchestration.export_session(db, session_id, format=format)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    media_type = "application/json" if format == "json" else "text/markdown"
    extension = "json" if format == "json" else "md"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=chat_{session_id}.{extension}"},
    )
