"""LLM provider credentials + real test-connection/model-discovery
(Settings redesign). Provider *order* is never a separate store here --
it's read straight from the live Agent Gateway config
(src.gateway.state.get_state(), the same infra.llmProviders.order
src.agents.llm_router.LlmRouter._fallback_order() already reads) so
Settings can never drift from what the router actually uses; writing the
order goes through the existing PUT /api/v1/gateway/config the same way
Skills already does (read-modify-write the whole config JSON), not a new
mutating endpoint here.

Provider *credentials* (api_key/base_url) are what this file actually
owns -- src.security.llm_provider_store, a real encrypted-at-rest store
this codebase never had before (env vars were the only option). Test and
model-discovery both build the exact same client class
src.agents.llm_router.default_clients() would build in production, so a
"pass" here means the real router would succeed too, not just that some
parallel code path works.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import (
    HOSTED_DEFAULT_MODELS,
    AnthropicClient,
    CustomProviderClient,
    GeminiClient,
    LlmProviderError,
    OllamaClient,
    OpenAiCompatibleClient,
    default_clients,
    stored_default_models,
)
from src.api.schemas import (
    LlmProviderModel,
    LlmProviderModelsResponse,
    LlmProviderStatusResponse,
    LlmProviderTestResult,
    WriteLlmProviderCredentialsRequest,
    WriteLlmProviderDefaultModelRequest,
)
from src.audit.service import write_audit_entry
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.gateway.schema import LlmProvider
from src.gateway.state import get_state
from src.models.user import User
from src.security.llm_provider_store import (
    LlmProviderCredentials,
    LlmProviderStore,
    LlmProviderStoreError,
    get_llm_provider_store,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/settings/llm-providers", tags=["llm-providers"])

_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]
# /test with no ?model= uses exactly what an agent's "auto" would: the
# provider's stored default model, else its built-in hosted default
# (src.agents.llm_router.HOSTED_DEFAULT_MODELS).

register_policy("GET", "/api/v1/settings/llm-providers", roles=list(Role))
register_policy("POST", "/api/v1/settings/llm-providers/{provider}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/settings/llm-providers/{provider}", roles=_WRITE_ROLES)
register_policy("POST", "/api/v1/settings/llm-providers/{provider}/test", roles=_WRITE_ROLES)
register_policy("GET", "/api/v1/settings/llm-providers/{provider}/models", roles=_WRITE_ROLES)
register_policy(
    "PUT", "/api/v1/settings/llm-providers/{provider}/default-model", roles=_WRITE_ROLES
)


def _get_store() -> LlmProviderStore:
    try:
        return get_llm_provider_store()
    except LlmProviderStoreError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def _require_known_provider(provider: str) -> LlmProvider:
    try:
        return LlmProvider(provider)
    except ValueError as exc:
        known = ", ".join(p.value for p in LlmProvider)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown provider: {provider!r} (known: {known})"
        ) from exc


def _current_order() -> list[LlmProvider]:
    config = get_state().get_config()
    if config is None:
        return list(LlmProvider)
    return list(config.infra.llm_providers.order)


@router.get("")
async def list_llm_provider_status_endpoint(
    _current_user: User = Depends(require_role),
    store: LlmProviderStore = Depends(_get_store),
) -> list[LlmProviderStatusResponse]:
    order = set(_current_order())
    responses = []
    for provider in LlmProvider:
        creds = store.get_credentials(provider.value)
        responses.append(
            LlmProviderStatusResponse(
                provider=provider.value,
                configured=creds is not None and bool(creds.api_key or creds.base_url),
                base_url=creds.base_url if creds else None,
                in_fallback_order=provider in order,
                default_model=creds.default_model if creds else None,
                builtin_default_model=HOSTED_DEFAULT_MODELS.get(provider),
            )
        )
    return responses


@router.post("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def write_llm_provider_credentials_endpoint(
    provider: str,
    body: WriteLlmProviderCredentialsRequest,
    current_user: User = Depends(require_role),
    store: LlmProviderStore = Depends(_get_store),
    db: AsyncSession = Depends(get_db),
) -> None:
    provider_enum = _require_known_provider(provider)
    if provider_enum == LlmProvider.CUSTOM and not body.base_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "custom/local providers require a base_url"
        )
    existing = store.get_credentials(provider)
    default_model = body.default_model or (existing.default_model if existing else None)
    store.set_credentials(
        provider,
        LlmProviderCredentials(
            api_key=body.api_key, base_url=body.base_url, default_model=default_model
        ),
    )
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="llm_provider_credentials.updated",
        entity_type="llm_provider_credentials",
        entity_id=provider,
    )
    await db.commit()
    logger.info(
        "llm_provider_credentials.updated", provider=provider, updated_by=str(current_user.id)
    )


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_provider_credentials_endpoint(
    provider: str,
    current_user: User = Depends(require_role),
    store: LlmProviderStore = Depends(_get_store),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_known_provider(provider)
    store.delete_credentials(provider)
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="llm_provider_credentials.deleted",
        entity_type="llm_provider_credentials",
        entity_id=provider,
    )
    await db.commit()
    logger.info(
        "llm_provider_credentials.deleted", provider=provider, deleted_by=str(current_user.id)
    )


@router.put("/{provider}/default-model", status_code=status.HTTP_204_NO_CONTENT)
async def write_llm_provider_default_model_endpoint(
    provider: str,
    body: WriteLlmProviderDefaultModelRequest,
    current_user: User = Depends(require_role),
    store: LlmProviderStore = Depends(_get_store),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Sets what `model: "auto"` resolves to on this provider, without
    re-entering its (write-only) key."""
    _require_known_provider(provider)
    creds = store.get_credentials(provider)
    if creds is None or not (creds.api_key or creds.base_url):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"configure {provider} (API key or base URL) before choosing its default model",
        )
    model = (body.model or "").strip() or None
    store.set_credentials(provider, replace(creds, default_model=model))
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="llm_provider_credentials.default_model_updated",
        entity_type="llm_provider_credentials",
        entity_id=provider,
        details={"default_model": model},
    )
    await db.commit()
    logger.info(
        "llm_provider_credentials.default_model_updated",
        provider=provider,
        default_model=model,
        updated_by=str(current_user.id),
    )


@router.post("/{provider}/test")
async def test_llm_provider_endpoint(
    provider: str,
    model: str | None = Query(default=None),
    current_user: User = Depends(require_role),
    db: AsyncSession = Depends(get_db),
) -> LlmProviderTestResult:
    provider_enum = _require_known_provider(provider)
    clients = default_clients()
    client = clients[provider_enum]

    test_model = (
        model
        or stored_default_models().get(provider_enum)
        or HOSTED_DEFAULT_MODELS.get(provider_enum)
    )
    if not test_model:
        # No universal default for a self-hosted server or Hugging Face --
        # pick one from /models (or save a default model) first.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "pass ?model=<name> (see GET .../models) or save a default model first",
        )

    started = time.monotonic()
    try:
        payload = await client.complete(model=test_model, prompt="Reply with the single word: ok")
    except (LlmProviderError, httpx.HTTPError) as exc:
        # httpx.HTTPError alongside LlmProviderError: OllamaClient/
        # CustomProviderClient already convert a connection failure into
        # LlmProviderError themselves (the common case -- a bare
        # `localhost` base URL from inside the backend's own Docker
        # container), but this is the safety net for any other client's
        # complete() that doesn't, so a network blip is a graceful
        # "ok: false" result here, never an unhandled 500.
        result = LlmProviderTestResult(provider=provider, ok=False, detail=str(exc))
    else:
        latency_ms = (time.monotonic() - started) * 1000
        result = LlmProviderTestResult(
            provider=provider,
            ok=True,
            detail=payload.text.strip()[:200] or "connected",
            latency_ms=round(latency_ms, 1),
        )

    await write_audit_entry(
        db,
        actor=current_user.email,
        action="llm_provider_credentials.tested",
        entity_type="llm_provider_credentials",
        entity_id=provider,
        details={"ok": result.ok, "model": test_model},
    )
    await db.commit()
    return result


async def get_discovery_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """A FastAPI dependency, not a bare `httpx.AsyncClient(...)` call, so
    tests can override it with a client bound to httpx.MockTransport --
    same "real code, injected transport in tests" posture as
    src.api.routes.broker_oauth."""
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        yield http_client


@router.get("/{provider}/models")
async def list_llm_provider_models_endpoint(
    provider: str,
    _current_user: User = Depends(require_role),
    http_client: httpx.AsyncClient = Depends(get_discovery_http_client),
) -> LlmProviderModelsResponse:
    provider_enum = _require_known_provider(provider)
    clients = default_clients()
    client = clients[provider_enum]

    try:
        models = await _discover_models(provider_enum, client, http_client)
    except (LlmProviderError, httpx.HTTPError) as exc:
        # httpx.HTTPError: _discover_models's per-provider branches don't
        # each convert a connection failure into LlmProviderError the way
        # OllamaClient.complete/CustomProviderClient.complete now do --
        # this is the single choke point that keeps a network blip (most
        # commonly a bare `localhost` base URL from inside the backend's
        # own Docker container reaching for a host-side Ollama instance)
        # a real 502 with a real message instead of an unhandled 500.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return LlmProviderModelsResponse(provider=provider, models=models)


async def _discover_models(
    provider: LlmProvider, client: object, http_client: httpx.AsyncClient
) -> list[LlmProviderModel]:
    """A real call to each provider's own model-listing API -- never a
    hardcoded list, since a hardcoded one goes stale the moment a provider
    ships a new model. Ollama's /api/tags is the one genuinely different
    shape (a local server, not a hosted API with its own auth)."""
    if isinstance(client, OllamaClient):
        resp = await http_client.get(f"{client.base_url}/api/tags")
        if resp.status_code != 200:
            raise LlmProviderError(f"ollama: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return [LlmProviderModel(id=m["name"], label=m.get("name")) for m in data.get("models", [])]

    if isinstance(client, CustomProviderClient):
        if not client.base_url:
            raise LlmProviderError("custom: no base URL configured")
        headers = {"Authorization": f"Bearer {client.api_key}"} if client.api_key else {}
        resp = await http_client.get(f"{client.base_url.rstrip('/')}/v1/models", headers=headers)
        if resp.status_code != 200:
            raise LlmProviderError(f"custom: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return [LlmProviderModel(id=m["id"]) for m in data.get("data", [])]

    if isinstance(client, AnthropicClient):
        if not client.api_key:
            raise LlmProviderError("anthropic: no API key configured")
        resp = await http_client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": client.api_key, "anthropic-version": "2023-06-01"},
        )
        if resp.status_code != 200:
            raise LlmProviderError(f"anthropic: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return [
            LlmProviderModel(id=m["id"], label=m.get("display_name")) for m in data.get("data", [])
        ]

    if isinstance(client, OpenAiCompatibleClient):
        if not client.api_key:
            raise LlmProviderError(f"{client.provider_name}: no API key configured")
        base = client.base_url.rsplit("/chat/completions", 1)[0]
        resp = await http_client.get(
            f"{base}/models", headers={"Authorization": f"Bearer {client.api_key}"}
        )
        if resp.status_code != 200:
            raise LlmProviderError(
                f"{client.provider_name}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return [LlmProviderModel(id=m["id"]) for m in data.get("data", [])]

    if isinstance(client, GeminiClient):
        if not client.api_key:
            raise LlmProviderError("gemini: no API key configured")
        resp = await http_client.get(client.base_url, params={"key": client.api_key})
        if resp.status_code != 200:
            raise LlmProviderError(f"gemini: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return [
            LlmProviderModel(id=m["name"].removeprefix("models/"), label=m.get("displayName"))
            for m in data.get("models", [])
        ]

    raise LlmProviderError(f"{provider.value}: model discovery not supported")
