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

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from src.core.config import get_settings
from src.gateway.schema import LlmProvider
from src.gateway.state import get_state
from src.observability.metrics import llm_token_usage_total

logger = structlog.get_logger(__name__)


class LlmProviderError(Exception):
    """Raised by an LlmProviderClient for any failed completion attempt —
    a bad/missing API key, a non-2xx response, a network error, anything.
    The router treats every LlmProviderError (and any other exception a
    client raises) identically: this provider failed, fall to the next.
    """


@dataclass(frozen=True, slots=True)
class LlmCompletionPayload:
    """What a single provider call actually returned -- the completion
    text plus real usage counts when the provider's own response includes
    them (Anthropic/OpenAI/DeepSeek/Ollama all report usage natively;
    Gemini's `usageMetadata` is likewise parsed where present). Neither
    token count is ever guessed or estimated when a provider's response
    doesn't carry them -- `None` means "not reported", not "zero", the
    same honesty this codebase applies to every other metric that can be
    genuinely undefined (see e.g. `src.engine.backtest.comparison`'s null
    correlation rule).
    """

    text: str
    prompt_tokens: int | None
    completion_tokens: int | None


class LlmProviderClient(Protocol):
    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload: ...


@dataclass(frozen=True, slots=True)
class AnthropicClient:
    api_key: str | None
    base_url: str = "https://api.anthropic.com/v1/messages"

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
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
            text = data["content"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(f"anthropic: unexpected response shape: {data!r}") from exc
        usage = data.get("usage") or {}
        return LlmCompletionPayload(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
        )


@dataclass(frozen=True, slots=True)
class OpenAiCompatibleClient:
    """OpenAI and DeepSeek both speak the same chat-completions wire
    format; DeepSeek is OpenAI-API-compatible at a different base URL.
    """

    api_key: str | None
    base_url: str
    provider_name: str

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
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
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(
                f"{self.provider_name}: unexpected response shape: {data!r}"
            ) from exc
        usage = data.get("usage") or {}
        return LlmCompletionPayload(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


@dataclass(frozen=True, slots=True)
class GeminiClient:
    api_key: str | None
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
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
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LlmProviderError(f"gemini: unexpected response shape: {data!r}") from exc
        usage = data.get("usageMetadata") or {}
        return LlmCompletionPayload(
            text=text,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
        )


@dataclass(frozen=True, slots=True)
class OllamaClient:
    base_url: str

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        if resp.status_code != 200:
            raise LlmProviderError(f"ollama: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            text = data["response"]
        except KeyError as exc:
            raise LlmProviderError(f"ollama: unexpected response shape: {data!r}") from exc
        return LlmCompletionPayload(
            text=text,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )


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
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmStreamChunk:
    """One increment of a streamed completion (Build Spec §18's in-app
    chat "streaming responses"). `text` is this chunk's delta only, never
    the accumulated text so far -- a consumer concatenates chunks itself.
    The final chunk for a call always has `done=True`; every field after
    `text` is `None`/absent until then, since only the last chunk carries
    the completed call's provider/fallback/usage summary (mirroring
    LlmCompletionResult).
    """

    text: str
    done: bool
    provider: LlmProvider | None = None
    used_fallback: bool | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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

    def _fallback_order(self, preferred_provider: LlmProvider | None = None) -> list[LlmProvider]:
        config = get_state().get_config()
        if config is None:
            # No Gateway config loaded yet (e.g. very early boot) — fall
            # back to declaration order rather than refusing to route.
            base_order = list(LlmProvider)
        else:
            base_order = list(config.infra.llm_providers.order)

        if preferred_provider is None:
            return base_order
        # Phase 12 (Build Spec §18): in-app chat's per-session model
        # switch -- a session pins a single preferred provider without
        # touching the global Gateway config, tried first with the rest
        # of the fallback chain kept intact as a safety net (never a
        # provider-or-nothing choice).
        rest = [p for p in base_order if p != preferred_provider]
        return [preferred_provider, *rest]

    async def complete(
        self,
        *,
        agent_id: str,
        prompt: str,
        model: str = "auto",
        preferred_provider: LlmProvider | None = None,
    ) -> LlmCompletionResult:
        order = self._fallback_order(preferred_provider)
        errors: list[tuple[LlmProvider, str]] = []

        for idx, provider in enumerate(order):
            client = self._clients.get(provider)
            if client is None:
                errors.append((provider, "no client configured for this provider"))
                continue
            try:
                payload = await client.complete(model=model, prompt=prompt)
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
            if payload.prompt_tokens is not None:
                llm_token_usage_total.labels(provider=provider.value, token_type="prompt").inc(
                    payload.prompt_tokens
                )
            if payload.completion_tokens is not None:
                llm_token_usage_total.labels(provider=provider.value, token_type="completion").inc(
                    payload.completion_tokens
                )
            return LlmCompletionResult(
                provider=provider,
                text=payload.text,
                used_fallback=used_fallback,
                failed_providers=tuple(p for p, _ in errors),
                prompt_tokens=payload.prompt_tokens,
                completion_tokens=payload.completion_tokens,
            )

        raise LlmRouterExhaustedError(agent_id, errors)

    async def stream_complete(
        self,
        *,
        agent_id: str,
        prompt: str,
        model: str = "auto",
        preferred_provider: LlmProvider | None = None,
    ) -> AsyncIterator[LlmStreamChunk]:
        """Streams a completion (Build Spec §18's in-app chat). Real,
        incremental SSE streaming for Anthropic (the only provider client
        this router streams natively) -- every other provider's client
        has no streaming call at all, so this falls back to running that
        provider's ordinary `complete()` and yielding its full text as
        word-chunks, an honestly-simulated stream sourced from one
        complete response rather than a fabricated multi-chunk provider
        stream. Same fallback-chain shape as `complete()`, with one
        genuine constraint streaming introduces that a single request/
        response call never has: **fallback to the next provider only
        happens before this provider has yielded its first chunk** --
        once real output has reached the caller, a later failure ends the
        stream with an exception instead of silently splicing in a
        different provider's content mid-response.
        """
        order = self._fallback_order(preferred_provider)
        errors: list[tuple[LlmProvider, str]] = []

        for idx, provider in enumerate(order):
            client = self._clients.get(provider)
            if client is None:
                errors.append((provider, "no client configured for this provider"))
                continue

            used_fallback = idx > 0
            any_yielded = False
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            try:
                if isinstance(client, AnthropicClient):
                    async for text_delta in _stream_anthropic(client, model=model, prompt=prompt):
                        any_yielded = True
                        yield LlmStreamChunk(text=text_delta, done=False)
                else:
                    payload = await client.complete(model=model, prompt=prompt)
                    prompt_tokens = payload.prompt_tokens
                    completion_tokens = payload.completion_tokens
                    for word_chunk in _word_chunks(payload.text):
                        any_yielded = True
                        yield LlmStreamChunk(text=word_chunk, done=False)
            except Exception as exc:  # noqa: BLE001 - any provider failure triggers fallback
                self._health[provider].last_failure_at = time.time()
                errors.append((provider, str(exc)))
                logger.warning(
                    "llm_router.provider_failed",
                    provider=provider.value,
                    agent_id=agent_id,
                    error=str(exc),
                )
                if any_yielded:
                    # Already streamed real content to the caller -- can't
                    # transparently splice in a different provider now.
                    raise
                continue

            self._health[provider].last_success_at = time.time()
            self._health[provider].served_as_fallback = used_fallback
            if prompt_tokens is not None:
                llm_token_usage_total.labels(provider=provider.value, token_type="prompt").inc(
                    prompt_tokens
                )
            if completion_tokens is not None:
                llm_token_usage_total.labels(provider=provider.value, token_type="completion").inc(
                    completion_tokens
                )
            yield LlmStreamChunk(
                text="",
                done=True,
                provider=provider,
                used_fallback=used_fallback,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return

        raise LlmRouterExhaustedError(agent_id, errors)


def _word_chunks(text: str) -> list[str]:
    """Splits a complete response into word-plus-trailing-space pieces so
    the non-natively-streaming fallback path in `stream_complete` still
    yields multiple chunks rather than the whole text at once -- a real
    (if coarse) stream, not a single disguised blob."""
    if not text:
        return []
    words = text.split(" ")
    return [w + " " for w in words[:-1]] + [words[-1]]


async def _stream_anthropic(
    client: AnthropicClient, *, model: str, prompt: str
) -> AsyncIterator[str]:
    """Real Anthropic Messages API SSE streaming (`"stream": true`),
    parsed by hand (no SDK dependency) -- yields each
    `content_block_delta` event's text piece as it arrives. Raises
    `LlmProviderError` (caught by `stream_complete`, same as the
    non-streaming path) for a non-200 response, checked *before* any
    chunk is yielded, so a bad key/network failure here falls back to the
    next provider exactly like `complete()` does.
    """
    if not client.api_key:
        raise LlmProviderError("anthropic: no API key configured")
    async with (
        httpx.AsyncClient(timeout=30.0) as http_client,
        http_client.stream(
            "POST",
            client.base_url,
            headers={
                "x-api-key": client.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as response,
    ):
        if response.status_code != 200:
            body = await response.aread()
            raise LlmProviderError(f"anthropic: HTTP {response.status_code}: {body[:200]!r}")
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[len("data: ") :].strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                text = delta.get("text")
                if text:
                    yield text


_router: LlmRouter | None = None


def get_llm_router() -> LlmRouter:
    global _router
    if _router is None:
        _router = LlmRouter()
    return _router
