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

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import (
    AnthropicClient,
    CustomProviderClient,
    GeminiClient,
    LlmProviderError,
    OllamaClient,
    OpenAiCompatibleClient,
    default_clients,
)
from src.api.schemas import (
    LlmProviderModel,
    LlmProviderModelsResponse,
    LlmProviderStatusResponse,
    LlmProviderTestResult,
    WriteLlmProviderCredentialsRequest,
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
# A minimal, cheap real completion for /test -- these are this endpoint's
# OWN default test model per provider, not necessarily what any agent's
# "auto" model resolves to; callers can override via ?model=.
_DEFAULT_TEST_MODELS = {
    LlmProvider.ANTHROPIC: "claude-3-5-haiku-20241022",
    LlmProvider.OPENAI: "gpt-4o-mini",
    LlmProvider.GEMINI: "gemini-1.5-flash",
    LlmProvider.DEEPSEEK: "deepseek-chat",
}

register_policy("GET", "/api/v1/settings/llm-providers", roles=list(Role))
register_policy("POST", "/api/v1/settings/llm-providers/{provider}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/settings/llm-providers/{provider}", roles=_WRITE_ROLES)
register_policy("POST", "/api/v1/settings/llm-providers/{provider}/test", roles=_WRITE_ROLES)
register_policy("GET", "/api/v1/settings/llm-providers/{provider}/models", roles=_WRITE_ROLES)


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
    store.set_credentials(
        provider, LlmProviderCredentials(api_key=body.api_key, base_url=body.base_url)
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

    test_model = model or _DEFAULT_TEST_MODELS.get(provider_enum, "default")
    if provider_enum in (LlmProvider.OLLAMA, LlmProvider.CUSTOM) and not model:
        # No universal default model name for a self-hosted server --
        # require the caller to pick one from /models first.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "pass ?model=<name> (see GET .../models) to test a local/custom provider",
        )

    started = time.monotonic()
    try:
        payload = await client.complete(model=test_model, prompt="Reply with the single word: ok")
    except LlmProviderError as exc:
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
    except LlmProviderError as exc:
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
