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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.memory.embeddings import SUPPORTED_PROVIDERS
from src.memory.service import (
    MAX_QUERY_LIMIT,
    MAX_TEXT_CHARS,
    MemoryService,
    MemoryUnavailableError,
    MemoryValidationError,
    get_memory_service,
)
from src.memory.settings_store import clear_choice, save_choice, source_from_factory
from src.memory.store import MEMORY_KINDS, MemoryStoreError
from src.models.user import User
from src.observability.audit_middleware import skip_audit_entry

router = APIRouter(prefix="/memory", tags=["memory"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("GET", "/api/v1/memory/status", roles=list(Role))
register_policy("GET", "/api/v1/memory/query", roles=list(Role))
register_policy("GET", "/api/v1/memory/settings", roles=list(Role))
register_policy("PUT", "/api/v1/memory/settings", roles=[Role.SYSTEM_ADMINISTRATOR])
register_policy("DELETE", "/api/v1/memory/settings", roles=[Role.SYSTEM_ADMINISTRATOR])
register_policy("POST", "/api/v1/memory/{kind}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/memory/{kind}/{memory_id}", roles=[Role.SYSTEM_ADMINISTRATOR])


def memory_service_dependency(
    request: Request, service: MemoryService = Depends(get_memory_service)
) -> MemoryService:
    """The shared service, pointed at this app's database for the operator's
    saved embedding choice (the session factory lives on the app, and is a
    different object per test)."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is not None:
        service.bind_override_source(source_from_factory(factory))
    return service


MemoryDep = Annotated[MemoryService, Depends(memory_service_dependency)]


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


class MemorySettingsResponse(BaseModel):
    provider: str
    model: str
    # "database" (saved in the UI), "environment" (MEMORY_EMBEDDING_* defaults)
    # or "unset" (nothing chosen yet).
    source: str
    updated_by: str | None
    updated_at: str | None
    supported_providers: list[str]


class MemorySettingsRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=200)


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


async def _settings_response(service: MemoryService) -> MemorySettingsResponse:
    provider, model, source, saved = await service.effective_choice()
    return MemorySettingsResponse(
        provider=provider,
        model=model,
        source=source,
        updated_by=saved.updated_by if saved else None,
        updated_at=saved.updated_at.isoformat() if saved else None,
        supported_providers=list(SUPPORTED_PROVIDERS),
    )


@router.get("/settings")
async def get_memory_settings(
    service: MemoryDep, _current_user: User = Depends(require_role)
) -> MemorySettingsResponse:
    return await _settings_response(service)


@router.put("/settings")
async def put_memory_settings(
    body: MemorySettingsRequest,
    service: MemoryDep,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> MemorySettingsResponse:
    """Persist the embedding provider + model in the database so they survive
    restarts without `.env`. Credentials are not stored here -- the provider's
    key/base URL still come from Settings > LLM providers."""
    provider = body.provider.strip().lower()
    model = body.model.strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unsupported provider {provider!r} (one of: {', '.join(SUPPORTED_PROVIDERS)})",
        )
    if not model:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "model must not be empty")
    await save_choice(db, provider=provider, model=model, actor=current_user.email)
    return await _settings_response(service)


@router.delete("/settings")
async def reset_memory_settings(
    request: Request,
    service: MemoryDep,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> MemorySettingsResponse:
    """Drop the saved choice; the MEMORY_EMBEDDING_* environment defaults (if
    any) apply again."""
    if not await clear_choice(db):
        # Idempotent by design (a repeat reset is a 200): but nothing was reset,
        # so the generic audit row must not say it was.
        skip_audit_entry(request, because="no saved embedding choice to reset")
    return await _settings_response(service)


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
