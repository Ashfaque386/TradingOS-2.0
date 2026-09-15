"""LLM router (Build Spec §7.1, §16): completes a prompt against a
multi-provider fallback chain read *live* from the Agent Gateway config
(`infra.llm_providers.order`, Phase 1) via src.gateway.state.get_state() —
the same hot-reloadable config surface the watcher already keeps current, so
reordering providers in config/tradingos.config.json takes effect on the
very next call with no restart, satisfying requirement 1 without any new
reload plumbing.

Every provider call goes through an LlmProviderClient (a thin httpx
wrapper below, one per provider). This sandbox has no provider API keys and
blocked egress to provider APIs, so the real clients are never exercised
here or in CI — same honest-stub posture as orchestration/planner.py's
fake_llm_planner(). Callers (real agent code, tests) can inject their own
`clients` mapping into LlmRouter to run entirely offline/deterministically;
production code uses the default real clients built from Settings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from src.core.config import get_settings
from src.gateway.schema import LlmProvider
from src.gateway.state import get_state

logger = structlog.get_logger(__name__)


class LlmProviderError(Exception):
    """Raised by an LlmProviderClient for any failed completion attempt —
    a bad/missing API key, a non-2xx response, a network error, anything.
    The router treats every LlmProviderError (and any other exception a
    client raises) identically: this provider failed, fall to the next.
    """


class LlmProviderClient(Protocol):
    async def complete(self, *, model: str, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class AnthropicClient:
    api_key: str | None
    base_url: str = "https://api.anthropic.com/v1/messages"

    async def complete(self, *, model: str, prompt: str) -> str:
        if not self.api_key:
            raise LlmProviderError("anthropic: no API key configured")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.base_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            raise LlmProviderError(f"anthropic: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(f"anthropic: unexpected response shape: {data!r}") from exc


@dataclass(frozen=True, slots=True)
class OpenAiCompatibleClient:
    """OpenAI and DeepSeek both speak the same chat-completions wire
    format; DeepSeek is OpenAI-API-compatible at a different base URL.
    """

    api_key: str | None
    base_url: str
    provider_name: str

    async def complete(self, *, model: str, prompt: str) -> str:
        if not self.api_key:
            raise LlmProviderError(f"{self.provider_name}: no API key configured")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            )
        if resp.status_code != 200:
            raise LlmProviderError(
                f"{self.provider_name}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(
                f"{self.provider_name}: unexpected response shape: {data!r}"
            ) from exc


@dataclass(frozen=True, slots=True)
class GeminiClient:
    api_key: str | None
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(self, *, model: str, prompt: str) -> str:
        if not self.api_key:
            raise LlmProviderError("gemini: no API key configured")
        url = f"{self.base_url}/{model}:generateContent"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={"key": self.api_key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
        if resp.status_code != 200:
            raise LlmProviderError(f"gemini: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(f"gemini: unexpected response shape: {data!r}") from exc


@dataclass(frozen=True, slots=True)
class OllamaClient:
    base_url: str

    async def complete(self, *, model: str, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        if resp.status_code != 200:
            raise LlmProviderError(f"ollama: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            return data["response"]
        except KeyError as exc:
            raise LlmProviderError(f"ollama: unexpected response shape: {data!r}") from exc


def default_clients() -> dict[LlmProvider, LlmProviderClient]:
    settings = get_settings()
    return {
        LlmProvider.ANTHROPIC: AnthropicClient(api_key=settings.anthropic_api_key),
        LlmProvider.OPENAI: OpenAiCompatibleClient(
            api_key=settings.openai_api_key,
            base_url="https://api.openai.com/v1/chat/completions",
            provider_name="openai",
        ),
        LlmProvider.GEMINI: GeminiClient(api_key=settings.gemini_api_key),
        LlmProvider.DEEPSEEK: OpenAiCompatibleClient(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com/chat/completions",
            provider_name="deepseek",
        ),
        LlmProvider.OLLAMA: OllamaClient(base_url=settings.ollama_base_url),
    }


@dataclass(slots=True)
class ProviderHealth:
    last_failure_at: float | None = None
    last_success_at: float | None = None
    # True if the most recent successful completion from this provider was
    # served as a fallback (i.e. it was not first in the config's order).
    served_as_fallback: bool = False


@dataclass(frozen=True, slots=True)
class LlmCompletionResult:
    provider: LlmProvider
    text: str
    used_fallback: bool
    failed_providers: tuple[LlmProvider, ...]


class LlmRouterExhaustedError(Exception):
    def __init__(self, agent_id: str, errors: list[tuple[LlmProvider, str]]):
        self.agent_id = agent_id
        self.errors = errors
        joined = "; ".join(f"{p.value}: {msg}" for p, msg in errors)
        super().__init__(f"llm_router: every provider failed for agent {agent_id!r}: {joined}")


class LlmRouter:
    """Stateful per-process router: holds per-provider failure-tracking
    health (requirement 1's "per-provider failure tracking") across calls.
    Fallback *order* is never cached here — _fallback_order() re-reads the
    live Gateway config every call, so a hot-reloaded config takes effect
    immediately without constructing a new router.
    """

    def __init__(self, clients: dict[LlmProvider, LlmProviderClient] | None = None) -> None:
        self._clients = clients if clients is not None else default_clients()
        self._health: dict[LlmProvider, ProviderHealth] = {
            provider: ProviderHealth() for provider in LlmProvider
        }

    def health_for(self, provider: LlmProvider) -> ProviderHealth:
        return self._health[provider]

    def _fallback_order(self) -> list[LlmProvider]:
        config = get_state().get_config()
        if config is None:
            # No Gateway config loaded yet (e.g. very early boot) — fall
            # back to declaration order rather than refusing to route.
            return list(LlmProvider)
        return list(config.infra.llm_providers.order)

    async def complete(
        self, *, agent_id: str, prompt: str, model: str = "auto"
    ) -> LlmCompletionResult:
        order = self._fallback_order()
        errors: list[tuple[LlmProvider, str]] = []

        for idx, provider in enumerate(order):
            client = self._clients.get(provider)
            if client is None:
                errors.append((provider, "no client configured for this provider"))
                continue
            try:
                text = await client.complete(model=model, prompt=prompt)
            except Exception as exc:  # noqa: BLE001 - any provider failure triggers fallback
                self._health[provider].last_failure_at = time.time()
                errors.append((provider, str(exc)))
                logger.warning(
                    "llm_router.provider_failed",
                    provider=provider.value,
                    agent_id=agent_id,
                    error=str(exc),
                )
                continue

            used_fallback = idx > 0
            self._health[provider].last_success_at = time.time()
            self._health[provider].served_as_fallback = used_fallback
            if used_fallback:
                logger.info(
                    "llm_router.served_via_fallback",
                    provider=provider.value,
                    agent_id=agent_id,
                    failed_providers=[p.value for p, _ in errors],
                )
            return LlmCompletionResult(
                provider=provider,
                text=text,
                used_fallback=used_fallback,
                failed_providers=tuple(p for p, _ in errors),
            )

        raise LlmRouterExhaustedError(agent_id, errors)


_router: LlmRouter | None = None


def get_llm_router() -> LlmRouter:
    global _router
    if _router is None:
        _router = LlmRouter()
    return _router
