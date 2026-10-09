"""Text embeddings for agent memory (docs/phase20-old-vs-new-comparison.md
item 18). A thin client per provider, built from credentials the operator has
already entered in Settings -- the encrypted LLM provider store first, then
the env-backed fields on `Settings`, the same order `src.agents.llm_router`
resolves a chat provider's credentials in -- so memory adds no second place to
paste a key.

There is deliberately no built-in fallback embedder. A hash, a bag-of-words
vector or anything else that needs no model would "work" and make similarity
search return confident-looking nonsense, which is worse than reporting that
memory is unavailable. `build_embedding_provider` therefore returns a reason
instead of a provider whenever it cannot build a real one, and the memory
service surfaces that reason verbatim.

Providers: Ollama (a locally pulled embedding model, e.g. `nomic-embed-text`),
OpenAI, Gemini, and Custom (any OpenAI-compatible `/v1/embeddings` server).
Hugging Face is left out on purpose: the old app's own audit found its free
tier rejects embedding models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from src.core.config import Settings
from src.security.llm_provider_store import LlmProviderCredentials, get_llm_provider_store

logger = structlog.get_logger(__name__)

SUPPORTED_PROVIDERS = ("ollama", "openai", "gemini", "custom")
_TIMEOUT_SECONDS = 60.0


class EmbeddingError(Exception):
    """An embedding call failed (connection, HTTP error, or a response that
    isn't the shape the provider documents)."""


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _validate(name: str, texts: list[str], vectors: object) -> list[list[float]]:
    """A provider's response is untrusted input: check the count, that every
    vector is a non-empty list of numbers, and that they all agree on a
    dimension, before anything is written to a vector store."""
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingError(f"{name}: expected {len(texts)} embeddings, got a different count")
    result: list[list[float]] = []
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or not vector
            or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vector)
        ):
            raise EmbeddingError(f"{name}: unexpected embedding shape in response")
        result.append([float(x) for x in vector])
    if len({len(v) for v in result}) != 1:
        raise EmbeddingError(f"{name}: embeddings have inconsistent dimensions")
    return result


async def _post(
    name: str,
    url: str,
    *,
    json: dict[str, object],
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=transport) as client:
            resp = await client.post(url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        # Same trap as the chat clients: a `localhost` base URL inside the
        # backend's own container means the container, not the host.
        raise EmbeddingError(f"{name}: connection failed: {exc}") from exc
    if resp.status_code != 200:
        raise EmbeddingError(f"{name}: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise EmbeddingError(f"{name}: response was not JSON") from exc
    if not isinstance(body, dict):
        raise EmbeddingError(f"{name}: unexpected response shape")
    return body


@dataclass
class OllamaEmbeddings:
    model: str
    base_url: str
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "ollama"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = await _post(
            self.name,
            f"{self.base_url.rstrip('/')}/api/embed",
            json={"model": self.model, "input": texts},
            transport=self.transport,
        )
        return _validate(self.name, texts, body.get("embeddings"))


@dataclass
class OpenAiCompatibleEmbeddings:
    """OpenAI itself, or any server exposing the same `POST .../embeddings`."""

    model: str
    url: str
    api_key: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "openai"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        body = await _post(
            self.name,
            self.url,
            json={"model": self.model, "input": texts},
            headers=headers,
            transport=self.transport,
        )
        data = body.get("data")
        if not isinstance(data, list):
            raise EmbeddingError(f"{self.name}: unexpected response shape")
        try:
            # The API tags each vector with the input position it answers.
            ordered = sorted(data, key=lambda item: item["index"])
            vectors: object = [item["embedding"] for item in ordered]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(f"{self.name}: unexpected response shape") from exc
        return _validate(self.name, texts, vectors)


@dataclass
class GeminiEmbeddings:
    model: str
    api_key: str
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "gemini"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = await _post(
            self.name,
            f"{self.base_url}/{self.model}:batchEmbedContents",
            # In a header, not the `?key=` query the chat client uses: a key
            # in a URL ends up in access logs and tracebacks.
            headers={"x-goog-api-key": self.api_key},
            json={
                "requests": [
                    {"model": f"models/{self.model}", "content": {"parts": [{"text": text}]}}
                    for text in texts
                ]
            },
            transport=self.transport,
        )
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingError("gemini: unexpected response shape")
        try:
            vectors: object = [item["values"] for item in embeddings]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("gemini: unexpected response shape") from exc
        return _validate(self.name, texts, vectors)


def _stored_credentials(provider: str) -> LlmProviderCredentials | None:
    try:
        return get_llm_provider_store().get_credentials(provider)
    except Exception:  # noqa: BLE001 - an unusable store just means "use the env fallbacks"
        return None


def build_embedding_provider(
    settings: Settings,
) -> tuple[EmbeddingProvider | None, str | None]:
    """The configured embedding provider, or (None, why not). The reason is
    written for an operator to act on: it names the exact setting to fix."""
    provider = settings.memory_embedding_provider.strip().lower()
    model = settings.memory_embedding_model.strip()
    if not provider:
        return None, "MEMORY_EMBEDDING_PROVIDER is not set (one of: " + ", ".join(
            SUPPORTED_PROVIDERS
        ) + ")"
    if provider not in SUPPORTED_PROVIDERS:
        return None, (
            f"MEMORY_EMBEDDING_PROVIDER={provider!r} is not supported "
            f"(one of: {', '.join(SUPPORTED_PROVIDERS)})"
        )
    if not model:
        return (
            None,
            f"MEMORY_EMBEDDING_MODEL is not set (the embedding model {provider} should use)",
        )

    stored = _stored_credentials(provider)

    if provider == "ollama":
        base_url = (
            stored.base_url if stored and stored.base_url else None
        ) or settings.ollama_base_url
        return OllamaEmbeddings(model=model, base_url=base_url), None

    if provider == "openai":
        api_key = (stored.api_key if stored and stored.api_key else None) or settings.openai_api_key
        if not api_key:
            return (
                None,
                "openai embeddings need an API key: add OpenAI in Settings or set OPENAI_API_KEY",
            )
        return (
            OpenAiCompatibleEmbeddings(
                model=model, url="https://api.openai.com/v1/embeddings", api_key=api_key
            ),
            None,
        )

    if provider == "gemini":
        api_key = (stored.api_key if stored and stored.api_key else None) or settings.gemini_api_key
        if not api_key:
            return (
                None,
                "gemini embeddings need an API key: add Gemini in Settings or set GEMINI_API_KEY",
            )
        return GeminiEmbeddings(model=model, api_key=api_key), None

    # custom
    if not (stored and stored.base_url):
        return None, "custom embeddings need a base URL: add the Custom provider in Settings"
    return (
        OpenAiCompatibleEmbeddings(
            model=model,
            url=f"{stored.base_url.rstrip('/')}/v1/embeddings",
            api_key=stored.api_key,
            name="custom",
        ),
        None,
    )
