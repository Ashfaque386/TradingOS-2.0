"""Phase 17 local Docker pass: making a configured provider actually usable
in the fallback chain.

- Every real agent call routes with `model="auto"`, which no client ever
  translated: a hosted provider was asked for a model literally named
  "auto". `resolve_model` maps it to the provider's stored default model,
  else its built-in hosted default, else fails that provider so the chain
  moves on.
- The router singleton built its clients once, so a key saved in Settings
  never reached an agent call until a restart. It now re-reads them each
  call.
- Hugging Face (Inference Providers, OpenAI-compatible) is a provider.
"""

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.agents import llm_router
from src.agents.llm_router import (
    HOSTED_DEFAULT_MODELS,
    LlmCompletionPayload,
    LlmProviderError,
    LlmRouter,
    OpenAiCompatibleClient,
    default_clients,
    resolve_model,
)
from src.api.routes.llm_providers import _get_store, get_discovery_http_client
from src.core.roles import Role
from src.gateway.apply import apply_config_text
from src.gateway.schema import LlmProvider
from src.main import app
from src.security.llm_provider_store import LlmProviderCredentials, LlmProviderStore

CONFIG_TEMPLATE = """
{{
  version: 1,
  infra: {{
    llmProviders: {{ order: [{order}] }},
    brokerFailover: {{ primary: 'zerodha', fallback: 'upstox' }},
    riskThresholdRefs: {{ maxDrawdownPct: 15, wsLatencyMs: 100 }},
  }},
}}
"""


def _config_with_order(*providers: str) -> str:
    return CONFIG_TEMPLATE.format(order=", ".join(f"'{p}'" for p in providers))


class _Recording:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
        self.models.append(model)
        return LlmCompletionPayload(text="ok", prompt_tokens=1, completion_tokens=1)


# ---- resolve_model ----------------------------------------------------


def test_explicit_model_passes_through():
    assert resolve_model(LlmProvider.OLLAMA, "llama3", {}) == "llama3"


def test_auto_uses_stored_default_over_builtin():
    defaults = {LlmProvider.OPENAI: "gpt-4.1"}
    assert resolve_model(LlmProvider.OPENAI, "auto", defaults) == "gpt-4.1"


def test_auto_falls_back_to_builtin_hosted_default():
    assert (
        resolve_model(LlmProvider.DEEPSEEK, "auto", {})
        == HOSTED_DEFAULT_MODELS[LlmProvider.DEEPSEEK]
    )


@pytest.mark.parametrize(
    "provider", [LlmProvider.OLLAMA, LlmProvider.CUSTOM, LlmProvider.HUGGINGFACE]
)
def test_auto_without_any_default_is_a_provider_failure(provider):
    with pytest.raises(LlmProviderError, match="no default model set"):
        resolve_model(provider, "auto", {})


# ---- router -----------------------------------------------------------


async def test_router_resolves_auto_per_provider_and_skips_ones_without_default(
    db_session_factory,
):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("huggingface", "ollama"), source="t")

    hf, ollama = _Recording(), _Recording()
    router = LlmRouter(
        clients={LlmProvider.HUGGINGFACE: hf, LlmProvider.OLLAMA: ollama},
        default_models={LlmProvider.OLLAMA: "qwen2.5:0.5b"},
    )
    result = await router.complete(agent_id="ceo-agent", prompt="hi")

    assert hf.models == []  # no default -> failed over, never asked for "auto"
    assert ollama.models == ["qwen2.5:0.5b"]
    assert result.provider == LlmProvider.OLLAMA
    assert result.failed_providers == (LlmProvider.HUGGINGFACE,)


async def test_router_streaming_also_resolves_auto(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("ollama"), source="t")

    ollama = _Recording()
    router = LlmRouter(
        clients={LlmProvider.OLLAMA: ollama},
        default_models={LlmProvider.OLLAMA: "qwen2.5:0.5b"},
    )
    chunks = [c async for c in router.stream_complete(agent_id="ceo-agent", prompt="hi")]
    assert chunks[-1].done and chunks[-1].provider == LlmProvider.OLLAMA
    assert ollama.models == ["qwen2.5:0.5b"]


async def test_router_rereads_clients_and_defaults_every_call(db_session_factory, monkeypatch):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("ollama"), source="t")

    built: list[_Recording] = []

    def fake_default_clients():
        client = _Recording()
        built.append(client)
        return {LlmProvider.OLLAMA: client}

    defaults = {LlmProvider.OLLAMA: "first-model"}
    monkeypatch.setattr(llm_router, "default_clients", fake_default_clients)
    monkeypatch.setattr(llm_router, "stored_default_models", lambda: dict(defaults))

    router = LlmRouter()
    await router.complete(agent_id="ceo-agent", prompt="hi")
    defaults[LlmProvider.OLLAMA] = "second-model"  # changed in Settings meanwhile
    await router.complete(agent_id="ceo-agent", prompt="hi")

    assert len(built) == 2
    assert built[0].models == ["first-model"]
    assert built[1].models == ["second-model"]


# ---- Hugging Face wiring -----------------------------------------------


def _store(tmp_path) -> LlmProviderStore:
    return LlmProviderStore(tmp_path / "llm.enc", Fernet.generate_key().decode())


def test_huggingface_client_uses_stored_token_and_hf_router(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set_credentials("huggingface", LlmProviderCredentials(api_key="hf_test"))
    monkeypatch.setattr(llm_router, "get_llm_provider_store", lambda: store)

    client = default_clients()[LlmProvider.HUGGINGFACE]
    assert isinstance(client, OpenAiCompatibleClient)
    assert client.api_key == "hf_test"
    assert client.base_url == "https://router.huggingface.co/v1/chat/completions"
    assert client.provider_name == "huggingface"


async def test_huggingface_accepted_in_gateway_fallback_order(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("huggingface", "anthropic"), source="t")
    assert LlmRouter(clients={}).fallback_order()[0] == LlmProvider.HUGGINGFACE


# ---- API ----------------------------------------------------------------


async def _admin(client: AsyncClient, make_user) -> dict:
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _override(store, monkeypatch):
    app.dependency_overrides[_get_store] = lambda: store
    monkeypatch.setattr(llm_router, "get_llm_provider_store", lambda: store)


def _clear():
    app.dependency_overrides.pop(_get_store, None)
    app.dependency_overrides.pop(get_discovery_http_client, None)


async def test_default_model_put_requires_configured_provider(
    client, make_user, tmp_path, monkeypatch
):
    _override(_store(tmp_path), monkeypatch)
    try:
        headers = await _admin(client, make_user)
        resp = await client.put(
            "/api/v1/settings/llm-providers/huggingface/default-model",
            json={"model": "meta-llama/Llama-3.1-8B-Instruct"},
            headers=headers,
        )
        assert resp.status_code == 400
    finally:
        _clear()


async def test_default_model_set_shown_kept_on_resave_and_cleared(
    client, make_user, tmp_path, monkeypatch
):
    store = _store(tmp_path)
    _override(store, monkeypatch)
    model = "meta-llama/Llama-3.1-8B-Instruct"
    try:
        headers = await _admin(client, make_user)
        base = "/api/v1/settings/llm-providers/huggingface"
        assert (
            await client.post(base, json={"api_key": "hf_a"}, headers=headers)
        ).status_code == 204
        resp = await client.put(f"{base}/default-model", json={"model": model}, headers=headers)
        assert resp.status_code == 204

        rows = {
            r["provider"]: r
            for r in (await client.get("/api/v1/settings/llm-providers", headers=headers)).json()
        }
        assert rows["huggingface"]["configured"] is True
        assert rows["huggingface"]["default_model"] == model
        assert rows["huggingface"]["builtin_default_model"] is None
        assert rows["openai"]["builtin_default_model"] == HOSTED_DEFAULT_MODELS[LlmProvider.OPENAI]

        # Re-entering the key must not lose the chosen model.
        assert (
            await client.post(base, json={"api_key": "hf_b"}, headers=headers)
        ).status_code == 204
        creds = store.get_credentials("huggingface")
        assert (creds.api_key, creds.default_model) == ("hf_b", model)

        resp = await client.put(f"{base}/default-model", json={"model": None}, headers=headers)
        assert resp.status_code == 204
        assert store.get_credentials("huggingface").default_model is None
        assert store.get_credentials("huggingface").api_key == "hf_b"
    finally:
        _clear()


async def test_test_endpoint_uses_saved_default_model(client, make_user, tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set_credentials(
        "huggingface",
        LlmProviderCredentials(api_key="hf_a", default_model="Qwen/Qwen2.5-7B-Instruct"),
    )
    _override(store, monkeypatch)
    recording = _Recording()
    monkeypatch.setattr(
        "src.api.routes.llm_providers.default_clients",
        lambda: {LlmProvider.HUGGINGFACE: recording},
    )
    try:
        headers = await _admin(client, make_user)
        resp = await client.post("/api/v1/settings/llm-providers/huggingface/test", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert recording.models == ["Qwen/Qwen2.5-7B-Instruct"]
    finally:
        _clear()


async def test_test_endpoint_without_any_model_for_huggingface_is_400(
    client, make_user, tmp_path, monkeypatch
):
    store = _store(tmp_path)
    store.set_credentials("huggingface", LlmProviderCredentials(api_key="hf_a"))
    _override(store, monkeypatch)
    try:
        headers = await _admin(client, make_user)
        resp = await client.post("/api/v1/settings/llm-providers/huggingface/test", headers=headers)
        assert resp.status_code == 400
    finally:
        _clear()


async def test_huggingface_model_discovery_hits_hf_router(client, make_user, tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set_credentials("huggingface", LlmProviderCredentials(api_key="hf_a"))
    _override(store, monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen2.5-7B-Instruct"}]})

    async def fake_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    app.dependency_overrides[get_discovery_http_client] = fake_client
    try:
        headers = await _admin(client, make_user)
        resp = await client.get(
            "/api/v1/settings/llm-providers/huggingface/models", headers=headers
        )
        assert resp.status_code == 200
        assert [m["id"] for m in resp.json()["models"]] == ["Qwen/Qwen2.5-7B-Instruct"]
        assert seen == {"url": "https://router.huggingface.co/v1/models", "auth": "Bearer hf_a"}
    finally:
        _clear()


async def test_default_model_put_requires_system_administrator(
    client, make_user, tmp_path, monkeypatch
):
    store = _store(tmp_path)
    store.set_credentials("ollama", LlmProviderCredentials(base_url="http://x:11434"))
    _override(store, monkeypatch)
    try:
        await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "pm@example.com", "password": "supersecret1"}
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        resp = await client.put(
            "/api/v1/settings/llm-providers/ollama/default-model",
            json={"model": "qwen2.5:0.5b"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert store.get_credentials("ollama").default_model is None
    finally:
        _clear()
