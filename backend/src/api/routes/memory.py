"""Agent long-term vector memory API (docs/phase20-old-vs-new-comparison.md
item 18). Status is readable by every role; writing a memory is limited to the
roles that can already run the pipeline that writes them, and deleting one is
admin-only. Reads are GETs on purpose: the audit middleware records every
mutating request, and a similarity query is not a mutation.

Everything degrades honestly: with no Qdrant server or no embedding provider
configured, `GET /memory/status` says so (200, `available: false`, the reason)
and the operations that need them return 503 with that same reason.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.core.rbac import Role, register_policy, require_role
from src.memory.service import (
    MAX_QUERY_LIMIT,
    MAX_TEXT_CHARS,
    MemoryService,
    MemoryUnavailableError,
    MemoryValidationError,
    get_memory_service,
)
from src.memory.store import MEMORY_KINDS, MemoryStoreError
from src.models.user import User

router = APIRouter(prefix="/memory", tags=["memory"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("GET", "/api/v1/memory/status", roles=list(Role))
register_policy("GET", "/api/v1/memory/query", roles=list(Role))
register_policy("POST", "/api/v1/memory/{kind}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/memory/{kind}/{memory_id}", roles=[Role.SYSTEM_ADMINISTRATOR])

MemoryDep = Annotated[MemoryService, Depends(get_memory_service)]


class CollectionStatusResponse(BaseModel):
    kind: str
    exists: bool
    count: int
    dimension: int | None


class MemoryStatusResponse(BaseModel):
    available: bool
    reason: str | None
    qdrant_configured: bool
    qdrant_reachable: bool
    embedding_provider: str | None
    embedding_model: str | None
    kinds: list[str]
    collections: list[CollectionStatusResponse]


class RememberRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)


class RememberResponse(BaseModel):
    id: str
    kind: str


class MemoryHitResponse(BaseModel):
    id: str
    kind: str
    score: float
    text: str
    metadata: dict[str, object]


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryUnavailableError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, MemoryValidationError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    # MemoryStoreError, notably a changed embedding dimension.
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/status")
async def memory_status(
    service: MemoryDep, _current_user: User = Depends(require_role)
) -> MemoryStatusResponse:
    s = await service.status()
    return MemoryStatusResponse(
        available=s.available,
        reason=s.reason,
        qdrant_configured=s.qdrant_configured,
        qdrant_reachable=s.qdrant_reachable,
        embedding_provider=s.embedding_provider,
        embedding_model=s.embedding_model,
        kinds=list(MEMORY_KINDS),
        collections=[
            CollectionStatusResponse(
                kind=c.kind, exists=c.exists, count=c.count, dimension=c.dimension
            )
            for c in s.collections
        ],
    )


@router.get("/query")
async def query_memory(
    service: MemoryDep,
    q: str = Query(min_length=1, max_length=MAX_TEXT_CHARS),
    kind: str | None = None,
    limit: int = Query(default=5, ge=1, le=MAX_QUERY_LIMIT),
    _current_user: User = Depends(require_role),
) -> list[MemoryHitResponse]:
    try:
        hits = await service.query(q, kind=kind, limit=limit)
    except (MemoryUnavailableError, MemoryValidationError, MemoryStoreError) as exc:
        raise _translate(exc) from exc
    return [
        MemoryHitResponse(id=hit.id, kind=k, score=hit.score, text=hit.text, metadata=hit.payload)
        for k, hit in hits
    ]


@router.post("/{kind}", status_code=status.HTTP_201_CREATED)
async def remember(
    kind: str,
    body: RememberRequest,
    service: MemoryDep,
    current_user: User = Depends(require_role),
) -> RememberResponse:
    try:
        memory_id = await service.remember(
            kind, body.text, {**body.metadata, "source": "operator", "author": current_user.email}
        )
    except (MemoryUnavailableError, MemoryValidationError, MemoryStoreError) as exc:
        raise _translate(exc) from exc
    return RememberResponse(id=memory_id, kind=kind)


@router.delete("/{kind}/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget(
    kind: str,
    memory_id: str,
    service: MemoryDep,
    _current_user: User = Depends(require_role),
) -> None:
    try:
        deleted = await service.forget(kind, memory_id)
    except (MemoryUnavailableError, MemoryValidationError, MemoryStoreError) as exc:
        raise _translate(exc) from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such memory")
