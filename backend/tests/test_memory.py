"""Agent long-term vector memory (docs/phase20-old-vs-new-comparison.md item
18): the embedding clients (against `httpx.MockTransport`), the Qdrant store and
`MemoryService` (against a real in-process `AsyncQdrantClient(":memory:")`, so
the vector search under test is real search), the LangGraph wiring, and the
HTTP routes.

The only fake is `_WordEmbedder` below -- a test-only embedder that hashes
words into a fixed-size bag-of-words vector so texts sharing words are near
each other. It exists purely so these tests can control similarity; the
production code has no such fallback (a missing embedding provider is reported
as "unavailable", asserted below).
"""

import hashlib
import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from qdrant_client import AsyncQdrantClient

from src.agents import graph as graph_module
from src.agents.graph import _with_active_prompt, run_pipeline
from src.agents.state import TradingOSGraphState
from src.core.config import Settings
from src.core.roles import Role
from src.main import app
from src.memory import embeddings as embeddings_module
from src.memory.embeddings import (
    EmbeddingError,
    GeminiEmbeddings,
    OllamaEmbeddings,
    OpenAiCompatibleEmbeddings,
    build_embedding_provider,
)
from src.memory.service import (
    MAX_TEXT_CHARS,
    MemoryService,
    MemoryUnavailableError,
    MemoryValidationError,
    PipelineMemory,
    get_memory_service,
)
from src.memory.store import DimensionMismatchError, QdrantMemoryStore
from src.security.llm_provider_store import LlmProviderCredentials

_DIM = 64


class _WordEmbedder:
    name = "test-words"
    model = "bag-of-words"

    def __init__(self, dim: int = _DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in text.lower().split():
                digest = hashlib.sha256(word.strip(".,:;'\"()").encode()).digest()
                vec[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
            if not any(vec):
                vec[0] = 1.0
            out.append(vec)
        return out


class _BrokenEmbedder:
    name = "broken"
    model = "none"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("broken: connection failed: refused")


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _service(embedder: Any = None, dim: int = _DIM) -> MemoryService:
    store = QdrantMemoryStore(AsyncQdrantClient(":memory:"))
    return MemoryService(_settings(), store=store, embedder=embedder or _WordEmbedder(dim))


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---- embedding clients ------------------------------------------------------


async def test_ollama_embeddings_request_and_response_shape():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[1, 2, 3], [4, 5, 6]]})

    client = OllamaEmbeddings("nomic-embed-text", "http://ollama:11434/", transport=_mock(handler))
    vectors = await client.embed(["a", "b"])

    assert seen["url"] == "http://ollama:11434/api/embed"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    assert vectors == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


async def test_openai_compatible_embeddings_orders_by_index_and_sends_bearer():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    client = OpenAiCompatibleEmbeddings(
        "text-embedding-3-small",
        "https://x/v1/embeddings",
        api_key="sk-test",
        transport=_mock(handler),
    )
    assert await client.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["auth"] == "Bearer sk-test"


async def test_openai_compatible_embeddings_omit_auth_header_without_a_key():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = OpenAiCompatibleEmbeddings("m", "http://local/v1/embeddings", transport=_mock(handler))
    await client.embed(["x"])
    assert seen["auth"] is None


async def test_gemini_embeddings_batch_request_with_key_in_header_not_url():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [{"values": [0.5, 0.5]}]})

    client = GeminiEmbeddings("text-embedding-004", "gem-key", transport=_mock(handler))
    assert await client.embed(["hello"]) == [[0.5, 0.5]]

    assert seen["url"].endswith("/models/text-embedding-004:batchEmbedContents")
    assert "gem-key" not in seen["url"]
    assert seen["key"] == "gem-key"
    assert seen["body"]["requests"][0]["content"] == {"parts": [{"text": "hello"}]}


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx.Response(401, text="bad key"), "HTTP 401"),
        (httpx.Response(200, text="not json"), "not JSON"),
        (httpx.Response(200, json=["a list"]), "unexpected response shape"),
        (httpx.Response(200, json={"embeddings": [[1.0]]}), "expected 2 embeddings"),
        (httpx.Response(200, json={"embeddings": [[1.0], [1.0, 2.0]]}), "inconsistent"),
        (httpx.Response(200, json={"embeddings": [[1.0], []]}), "unexpected embedding shape"),
        (httpx.Response(200, json={"embeddings": [["x"], [1.0]]}), "unexpected embedding shape"),
        (httpx.Response(200, json={"embeddings": [[True], [1.0]]}), "unexpected embedding shape"),
    ],
)
async def test_embedding_client_rejects_a_bad_response(response: httpx.Response, needle: str):
    client = OllamaEmbeddings("m", "http://o", transport=_mock(lambda request: response))
    with pytest.raises(EmbeddingError, match=needle):
        await client.embed(["a", "b"])


async def test_embedding_client_wraps_connection_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = OllamaEmbeddings("m", "http://o", transport=_mock(handler))
    with pytest.raises(EmbeddingError, match="connection failed"):
        await client.embed(["a"])


# ---- build_embedding_provider ----------------------------------------------


def _no_stored_credentials(
    monkeypatch: pytest.MonkeyPatch, creds: LlmProviderCredentials | None = None
):
    class _Store:
        def get_credentials(self, provider: str) -> LlmProviderCredentials | None:
            return creds

    monkeypatch.setattr(embeddings_module, "get_llm_provider_store", lambda: _Store())


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({}, "MEMORY_EMBEDDING_PROVIDER is not set"),
        ({"memory_embedding_provider": "huggingface"}, "not supported"),
        ({"memory_embedding_provider": "ollama"}, "MEMORY_EMBEDDING_MODEL is not set"),
        ({"memory_embedding_provider": "openai", "memory_embedding_model": "m"}, "OPENAI_API_KEY"),
        ({"memory_embedding_provider": "gemini", "memory_embedding_model": "m"}, "GEMINI_API_KEY"),
        ({"memory_embedding_provider": "custom", "memory_embedding_model": "m"}, "base URL"),
    ],
)
def test_unconfigured_embedding_provider_reports_exactly_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], needle: str
):
    _no_stored_credentials(monkeypatch)
    provider, reason = build_embedding_provider(_settings(**overrides))
    assert provider is None
    assert reason is not None and needle in reason


def test_there_is_no_fallback_embedder(monkeypatch: pytest.MonkeyPatch):
    _no_stored_credentials(monkeypatch)
    provider, _ = build_embedding_provider(_settings())
    assert provider is None


def test_embedding_provider_resolution_uses_env_then_prefers_stored_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    _no_stored_credentials(monkeypatch)
    env = _settings(
        memory_embedding_provider="openai", memory_embedding_model="m", openai_api_key="env-key"
    )
    provider, _ = build_embedding_provider(env)
    assert isinstance(provider, OpenAiCompatibleEmbeddings) and provider.api_key == "env-key"

    _no_stored_credentials(monkeypatch, LlmProviderCredentials(api_key="stored-key"))
    provider, _ = build_embedding_provider(env)
    assert isinstance(provider, OpenAiCompatibleEmbeddings) and provider.api_key == "stored-key"


def test_custom_and_ollama_providers_resolve_their_base_urls(monkeypatch: pytest.MonkeyPatch):
    _no_stored_credentials(monkeypatch, LlmProviderCredentials(base_url="http://llm.local:8080/"))
    custom, _ = build_embedding_provider(
        _settings(memory_embedding_provider="custom", memory_embedding_model="m")
    )
    assert isinstance(custom, OpenAiCompatibleEmbeddings)
    assert custom.url == "http://llm.local:8080/v1/embeddings" and custom.name == "custom"

    ollama, _ = build_embedding_provider(
        _settings(memory_embedding_provider="ollama", memory_embedding_model="nomic-embed-text")
    )
    assert isinstance(ollama, OllamaEmbeddings) and ollama.base_url == "http://llm.local:8080/"


def test_an_unusable_credential_store_falls_back_to_env(monkeypatch: pytest.MonkeyPatch):
    def boom() -> Any:
        raise RuntimeError("SECRETS_ENCRYPTION_KEY is not set")

    monkeypatch.setattr(embeddings_module, "get_llm_provider_store", boom)
    provider, _ = build_embedding_provider(
        _settings(memory_embedding_provider="ollama", memory_embedding_model="m")
    )
    assert isinstance(provider, OllamaEmbeddings)


# ---- store + service against real (in-process) Qdrant ------------------------


async def test_remember_then_query_ranks_the_relevant_memory_first():
    svc = _service()
    await svc.remember("strategy", "momentum breakout on NIFTY failed in sideways markets")
    await svc.remember("strategy", "mean reversion on banking stocks worked with tight stops")
    await svc.remember("strategy", "options straddle before earnings crushed by volatility")

    hits = await svc.query("momentum breakout NIFTY sideways", kind="strategy", limit=3)

    assert [kind for kind, _ in hits] == ["strategy"] * 3
    assert "momentum breakout" in hits[0][1].text
    assert hits[0][1].score > hits[1][1].score >= hits[2][1].score


async def test_query_with_no_kind_searches_every_collection_and_merges_by_score():
    svc = _service()
    await svc.remember("strategy", "gold rally momentum")
    await svc.remember("news", "gold prices surge on rate cut hopes")
    await svc.remember("organization", "quarterly planning notes")

    hits = await svc.query("gold prices surge", limit=5)

    assert {kind for kind, _ in hits} >= {"strategy", "news"}
    assert hits[0][0] == "news"
    assert [h.score for _, h in hits] == sorted((h.score for _, h in hits), reverse=True)


async def test_metadata_round_trips_and_text_is_not_duplicated_into_it():
    svc = _service()
    await svc.remember("news", "RBI holds repo rate", {"source": "operator", "importance": 3})

    ((_, hit),) = await svc.query("RBI repo rate", kind="news")

    assert hit.text == "RBI holds repo rate"
    assert hit.payload["importance"] == 3 and hit.payload["source"] == "operator"
    assert "text" not in hit.payload and "created_at" in hit.payload


async def test_querying_a_never_written_kind_is_empty_not_an_error():
    assert await _service().query("anything", kind="news") == []


async def test_forget_deletes_one_memory_and_reports_a_missing_one():
    svc = _service()
    keep = await svc.remember("news", "keep this one about gold")
    drop = await svc.remember("news", "drop this one about gold")

    assert await svc.forget("news", drop) is True
    assert await svc.forget("news", drop) is False
    assert await svc.forget("strategy", drop) is False  # collection never created

    remaining = await svc.query("gold", kind="news")
    assert [hit.id for _, hit in remaining] == [keep]


async def test_validation_rejects_unknown_kinds_and_bad_text():
    svc = _service()
    with pytest.raises(MemoryValidationError, match="unknown memory kind"):
        await svc.remember("diary", "x")
    with pytest.raises(MemoryValidationError, match="must not be empty"):
        await svc.remember("news", "   ")
    with pytest.raises(MemoryValidationError, match="limit"):
        await svc.remember("news", "x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(MemoryValidationError, match="unknown memory kind"):
        await svc.query("x", kind="diary")


async def test_changing_the_embedding_dimension_is_reported_not_papered_over():
    store = QdrantMemoryStore(AsyncQdrantClient(":memory:"))
    first = MemoryService(_settings(), store=store, embedder=_WordEmbedder(64))
    await first.remember("news", "written with 64 dimensions")
    second = MemoryService(_settings(), store=store, embedder=_WordEmbedder(32))

    with pytest.raises(DimensionMismatchError, match="64-dimension"):
        await second.remember("news", "now 32 dimensions")
    with pytest.raises(DimensionMismatchError):
        await second.query("anything", kind="news")
    # ...and the original data is untouched.
    assert len(await first.query("64 dimensions", kind="news")) == 1


async def test_status_reports_collection_counts_and_dimension():
    svc = _service()
    await svc.remember("news", "one")
    await svc.remember("news", "two")

    status = await svc.status()

    assert status.available is True and status.reason is None
    assert (status.embedding_provider, status.embedding_model) == ("test-words", "bag-of-words")
    by_kind = {c.kind: c for c in status.collections}
    assert (by_kind["news"].exists, by_kind["news"].count, by_kind["news"].dimension) == (
        True,
        2,
        _DIM,
    )
    assert by_kind["strategy"].exists is False and by_kind["strategy"].count == 0


async def test_unconfigured_memory_is_honestly_unavailable(monkeypatch: pytest.MonkeyPatch):
    _no_stored_credentials(monkeypatch)
    svc = MemoryService(_settings())

    status = await svc.status()
    assert status.available is False
    assert status.qdrant_configured is False and status.qdrant_reachable is False
    assert status.reason is not None
    assert "QDRANT_URL is not set" in status.reason
    assert "MEMORY_EMBEDDING_PROVIDER is not set" in status.reason

    with pytest.raises(MemoryUnavailableError, match="QDRANT_URL"):
        await svc.remember("news", "x")
    with pytest.raises(MemoryUnavailableError):
        await svc.query("x")


async def test_embedding_failure_surfaces_as_unavailable_not_a_crash():
    svc = MemoryService(
        _settings(),
        store=QdrantMemoryStore(AsyncQdrantClient(":memory:")),
        embedder=_BrokenEmbedder(),
    )
    with pytest.raises(MemoryUnavailableError, match="embedding failed"):
        await svc.remember("news", "x")


async def test_status_probes_the_embedding_model_rather_than_trusting_config():
    # A provider that is configured but whose model cannot embed (an Ollama
    # chat model answers 501) must not read as "available".
    svc = MemoryService(
        _settings(),
        store=QdrantMemoryStore(AsyncQdrantClient(":memory:")),
        embedder=_BrokenEmbedder(),
    )
    status = await svc.status()
    assert status.available is False
    assert status.reason is not None and "embedding failed" in status.reason
    assert status.qdrant_reachable is True


async def test_status_probe_is_cached_so_refreshes_do_not_hammer_the_provider():
    embedder = _WordEmbedder()
    svc = MemoryService(
        _settings(), store=QdrantMemoryStore(AsyncQdrantClient(":memory:")), embedder=embedder
    )
    await svc.status()
    await svc.status()
    assert len(embedder.calls) == 1


async def test_unreachable_qdrant_is_reported_in_status(monkeypatch: pytest.MonkeyPatch):
    _no_stored_credentials(monkeypatch)
    svc = MemoryService(
        _settings(
            qdrant_url="http://127.0.0.1:1",
            memory_embedding_provider="ollama",
            memory_embedding_model="m",
        )
    )
    status = await svc.status()
    assert status.qdrant_configured is True and status.qdrant_reachable is False
    assert status.available is False
    assert status.reason is not None and "vector store" in status.reason
    await svc.aclose()


# ---- PipelineMemory + graph wiring -------------------------------------------


async def test_pipeline_memory_recalls_only_pipeline_lessons_up_to_the_limit():
    svc = _service()
    for i in range(4):
        await svc.remember("strategy", f"momentum lesson number {i}")
    await svc.remember("news", "momentum headline that is not a lesson")

    recalled = await PipelineMemory(svc, recall_limit=3).recall("momentum")

    assert len(recalled) == 3
    assert all(text.startswith("momentum lesson") for text in recalled)
    assert await PipelineMemory(svc, recall_limit=0).recall("momentum") == []


async def test_pipeline_memory_swallows_every_failure(monkeypatch: pytest.MonkeyPatch):
    _no_stored_credentials(monkeypatch)
    memory = PipelineMemory(MemoryService(_settings()), recall_limit=3)  # nothing configured
    assert await memory.recall("anything") == []
    await memory.remember("a lesson", {})  # must not raise


class _RecordingMemory:
    def __init__(self, lessons: list[str] | None = None) -> None:
        self.lessons = lessons or []
        self.recalled: list[str] = []
        self.remembered: list[tuple[str, dict[str, Any]]] = []

    async def recall(self, objective: str) -> list[str]:
        self.recalled.append(objective)
        return self.lessons

    async def remember(self, text: str, metadata: dict[str, Any]) -> None:
        self.remembered.append((text, metadata))


def test_recalled_lessons_are_appended_to_prompts_after_the_task():
    state = TradingOSGraphState(objective="o", memory_context=["lesson one", "lesson two"])
    prompt = _with_active_prompt(state, "ceo-agent", "Do the task")
    assert prompt.startswith("Do the task")
    assert "- lesson one\n- lesson two" in prompt


def test_prompts_are_unchanged_without_recalled_lessons():
    state = TradingOSGraphState(objective="o")
    assert _with_active_prompt(state, "ceo-agent", "Do the task") == "Do the task"


async def test_run_pipeline_recalls_up_front_and_records_the_outcome():
    memory = _RecordingMemory(["earlier lesson"])

    state = await run_pipeline("Build a momentum strategy", run_id="run-1", memory=memory)

    assert memory.recalled == ["Build a momentum strategy"]
    assert state.memory_context == ["earlier lesson"]
    ((text, metadata),) = memory.remembered
    assert "Build a momentum strategy" in text and "deployed to paper" in text
    assert metadata["event"] == "outcome" and metadata["run_id"] == "run-1"
    assert metadata["verdict"] == "pass" and metadata["rejections"] == 0


async def test_each_rejection_is_remembered_by_the_memory_ingest_node(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = {"n": 0}

    async def _backtest(state: TradingOSGraphState, router: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "backtest_metrics": {"sharpe": 0.0 if calls["n"] <= 2 else 1.0},
            "node_log": [*state.node_log, "backtesting"],
        }

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "backtesting", _backtest)
    memory = _RecordingMemory()

    state = await run_pipeline("obj", memory=memory)

    assert state.rejection_count == 2
    events = [meta["event"] for _, meta in memory.remembered]
    assert events == ["rejection", "rejection", "outcome"]
    assert "rejected at evaluation" in memory.remembered[0][0]


async def test_a_raising_memory_never_breaks_the_run():
    class _Exploding:
        async def recall(self, objective: str) -> list[str]:
            raise RuntimeError("recall exploded")

        async def remember(self, text: str, metadata: dict[str, Any]) -> None:
            raise RuntimeError("remember exploded")

    state = await run_pipeline("obj", memory=_Exploding())

    assert state.deployment_result is not None and state.memory_context == []


async def test_without_memory_the_pipeline_is_unchanged():
    state = await run_pipeline("obj")
    assert state.memory_context == [] and state.deployment_result is not None


# ---- HTTP routes -------------------------------------------------------------


@pytest.fixture
def memory_service():
    svc = _service()
    app.dependency_overrides[get_memory_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_memory_service, None)


async def _login(client: AsyncClient, make_user: Any, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_status_is_readable_by_every_role(client, make_user, memory_service):
    headers = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "auditor-mem@example.com")

    resp = await client.get("/api/v1/memory/status", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True and body["kinds"] == ["strategy", "news", "organization"]
    assert body["embedding_provider"] == "test-words"


async def test_remember_query_and_forget_round_trip(client, make_user, memory_service):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-mem@example.com")

    created = await client.post(
        "/api/v1/memory/news",
        json={"text": "RBI keeps repo rate unchanged", "metadata": {"importance": 2}},
        headers=admin,
    )
    assert created.status_code == 201
    memory_id = created.json()["id"]

    found = await client.get(
        "/api/v1/memory/query", params={"q": "RBI repo rate", "kind": "news"}, headers=admin
    )
    assert found.status_code == 200
    (hit,) = found.json()
    assert hit["id"] == memory_id and hit["kind"] == "news"
    assert hit["metadata"]["importance"] == 2
    assert hit["metadata"]["author"] == "admin-mem@example.com"
    assert hit["metadata"]["source"] == "operator"

    assert (
        await client.delete(f"/api/v1/memory/news/{memory_id}", headers=admin)
    ).status_code == 204
    assert (
        await client.delete(f"/api/v1/memory/news/{memory_id}", headers=admin)
    ).status_code == 404


async def test_roles_are_enforced_on_writes_and_deletes(client, make_user, memory_service):
    pm = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-mem@example.com")
    auditor = await _login(client, make_user, Role.READ_ONLY_AUDITOR, "aud2-mem@example.com")

    assert (
        await client.post("/api/v1/memory/news", json={"text": "x"}, headers=auditor)
    ).status_code == 403
    created = await client.post("/api/v1/memory/news", json={"text": "pm note"}, headers=pm)
    assert created.status_code == 201
    # Deleting is admin-only: a portfolio manager can add memories, not erase them.
    assert (
        await client.delete(f"/api/v1/memory/news/{created.json()['id']}", headers=pm)
    ).status_code == 403
    assert (await client.get("/api/v1/memory/query", params={"q": "pm"})).status_code == 401


async def test_validation_errors_map_to_422_and_unknown_kind_is_rejected(
    client, make_user, memory_service
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-val@example.com")

    bad_kind = await client.post("/api/v1/memory/diary", json={"text": "x"}, headers=admin)
    assert bad_kind.status_code == 422 and "unknown memory kind" in bad_kind.json()["detail"]
    assert (
        await client.post("/api/v1/memory/news", json={"text": ""}, headers=admin)
    ).status_code == 422
    assert (
        await client.get("/api/v1/memory/query", params={"q": "x", "limit": 999}, headers=admin)
    ).status_code == 422


async def test_unavailable_memory_returns_200_status_and_503_operations(
    client, make_user, monkeypatch: pytest.MonkeyPatch
):
    _no_stored_credentials(monkeypatch)
    app.dependency_overrides[get_memory_service] = lambda: MemoryService(_settings())
    try:
        admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-off@example.com")

        status = await client.get("/api/v1/memory/status", headers=admin)
        assert status.status_code == 200
        assert status.json()["available"] is False
        assert "QDRANT_URL is not set" in status.json()["reason"]

        write = await client.post("/api/v1/memory/news", json={"text": "x"}, headers=admin)
        assert write.status_code == 503 and "QDRANT_URL" in write.json()["detail"]
        read = await client.get("/api/v1/memory/query", params={"q": "x"}, headers=admin)
        assert read.status_code == 503
    finally:
        app.dependency_overrides.pop(get_memory_service, None)


async def test_dimension_conflict_maps_to_409(client, make_user):
    store = QdrantMemoryStore(AsyncQdrantClient(":memory:"))
    await MemoryService(_settings(), store=store, embedder=_WordEmbedder(64)).remember(
        "news", "seed"
    )
    narrow = MemoryService(_settings(), store=store, embedder=_WordEmbedder(32))
    app.dependency_overrides[get_memory_service] = lambda: narrow
    try:
        admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-dim@example.com")
        resp = await client.post("/api/v1/memory/news", json={"text": "x"}, headers=admin)
        assert resp.status_code == 409 and "64-dimension" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_memory_service, None)


async def test_pipeline_run_endpoint_still_works_with_memory_unconfigured(client, make_user):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-run-mem@example.com")
    resp = await client.post(
        "/api/v1/agents/pipeline/run", json={"objective": "memory off"}, headers=admin
    )
    assert resp.status_code == 200 and "deployment" in resp.json()["node_log"]
