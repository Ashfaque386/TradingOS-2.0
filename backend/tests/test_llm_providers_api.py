"""LLM provider Settings API tests (Settings redesign): credential
write-only round-trip, /test using the exact same client default_clients()
would build for the real router (a provider with no key configured fails
before any network call, so this is exercised without egress), and
/models discovery against an injected httpx.MockTransport -- same posture
as test_broker_oauth_api.py.

default_clients() (src.agents.llm_router) reads the LLM provider store via
its own direct call to get_llm_provider_store() -- a process-wide
@lru_cache singleton, not a FastAPI dependency -- so overriding
_get_store (llm_providers.py's Depends(...) target for the list/write/
delete endpoints) does NOT reach /test or /models, which call
default_clients() directly. _override_store below patches both: the
Depends target for CRUD endpoints, and the name as imported into
src.agents.llm_router for /test and /models, the same "patch where it's
used" fix already applied to the analogous src.api.routes.broker_oauth
bug this suite caught earlier.
"""

import httpx
from httpx import AsyncClient

from src.api.routes.llm_providers import _get_store, get_discovery_http_client
from src.core.roles import Role
from src.gateway.schema import LlmProvider
from src.main import app
from src.security.llm_provider_store import LlmProviderStore


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_store(tmp_path, monkeypatch) -> LlmProviderStore:
    from cryptography.fernet import Fernet

    store = LlmProviderStore(tmp_path / "llm.enc", Fernet.generate_key().decode())
    app.dependency_overrides[_get_store] = lambda: store
    monkeypatch.setattr("src.agents.llm_router.get_llm_provider_store", lambda: store)
    return store


def _override_discovery_client(handler):
    async def _fake_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    app.dependency_overrides[get_discovery_http_client] = _fake_client


def _clear_overrides():
    app.dependency_overrides.pop(_get_store, None)
    app.dependency_overrides.pop(get_discovery_http_client, None)


async def test_list_status_allowed_for_any_role(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "auditor@example.com", "supersecret1")

        resp = await client.get("/api/v1/settings/llm-providers", headers=_auth(token))
        assert resp.status_code == 200
        providers = {row["provider"] for row in resp.json()}
        assert providers == {p.value for p in LlmProvider}
    finally:
        _clear_overrides()


async def test_write_requires_system_administrator(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "pm@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/anthropic",
            json={"api_key": "sk-test"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_overrides()


async def test_write_then_list_never_echoes_api_key(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin@example.com", "supersecret1")

        write_resp = await client.post(
            "/api/v1/settings/llm-providers/anthropic",
            json={"api_key": "sk-super-secret"},
            headers=_auth(token),
        )
        assert write_resp.status_code == 204

        list_resp = await client.get("/api/v1/settings/llm-providers", headers=_auth(token))
        assert "sk-super-secret" not in list_resp.text

        by_provider = {row["provider"]: row for row in list_resp.json()}
        assert by_provider["anthropic"]["configured"] is True
        assert by_provider["openai"]["configured"] is False
    finally:
        _clear_overrides()


async def test_write_custom_provider_requires_base_url(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/custom", json={}, headers=_auth(token)
        )
        assert resp.status_code == 400
    finally:
        _clear_overrides()


async def test_write_custom_provider_with_base_url_only(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin3@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/custom",
            json={"base_url": "http://localhost:11434"},
            headers=_auth(token),
        )
        assert resp.status_code == 204

        list_resp = await client.get("/api/v1/settings/llm-providers", headers=_auth(token))
        by_provider = {row["provider"]: row for row in list_resp.json()}
        assert by_provider["custom"]["configured"] is True
        assert by_provider["custom"]["base_url"] == "http://localhost:11434"
    finally:
        _clear_overrides()


async def test_unknown_provider_is_404(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin4@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/not-a-real-provider",
            json={"api_key": "x"},
            headers=_auth(token),
        )
        assert resp.status_code == 404
    finally:
        _clear_overrides()


async def test_delete_removes_credentials(client, make_user, tmp_path, monkeypatch):
    store = _override_store(tmp_path, monkeypatch)
    try:
        from src.security.llm_provider_store import LlmProviderCredentials

        store.set_credentials("openai", LlmProviderCredentials(api_key="sk-x"))

        await make_user("admin5@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin5@example.com", "supersecret1")

        resp = await client.delete("/api/v1/settings/llm-providers/openai", headers=_auth(token))
        assert resp.status_code == 204
        assert store.get_credentials("openai") is None
    finally:
        _clear_overrides()


async def test_test_connection_without_credentials_reports_failure_not_500(
    client, make_user, tmp_path, monkeypatch
):
    """No stored credentials and no env var -- AnthropicClient.complete()
    raises LlmProviderError before any network call, so this is a fully
    deterministic real code path even with zero egress in this sandbox.
    """
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin6@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin6@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/anthropic/test", headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "no API key configured" in body["detail"]
    finally:
        _clear_overrides()


async def test_test_connection_local_provider_requires_model_query_param(
    client, make_user, tmp_path, monkeypatch
):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin7@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin7@example.com", "supersecret1")

        resp = await client.post("/api/v1/settings/llm-providers/ollama/test", headers=_auth(token))
        assert resp.status_code == 400
    finally:
        _clear_overrides()


async def test_test_connection_unknown_provider_is_404(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path, monkeypatch)
    try:
        await make_user("admin8@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin8@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/settings/llm-providers/not-real/test", headers=_auth(token)
        )
        assert resp.status_code == 404
    finally:
        _clear_overrides()


async def test_models_discovery_ollama_real_api_tags_shape(
    client, make_user, tmp_path, monkeypatch
):
    from src.agents.llm_router import OllamaClient

    _override_store(tmp_path, monkeypatch)

    def fake_default_clients():
        return {LlmProvider.OLLAMA: OllamaClient(base_url="http://fake-ollama:11434")}

    monkeypatch.setattr("src.api.routes.llm_providers.default_clients", fake_default_clients)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3:8b"}, {"name": "mistral:7b"}]})

    _override_discovery_client(handler)
    try:
        await make_user("admin9@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin9@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/settings/llm-providers/ollama/models", headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {m["id"] for m in body["models"]} == {"llama3:8b", "mistral:7b"}
    finally:
        _clear_overrides()


async def test_models_discovery_custom_provider_openai_compatible_shape(
    client, make_user, tmp_path, monkeypatch
):
    from src.agents.llm_router import CustomProviderClient

    _override_store(tmp_path, monkeypatch)

    def fake_default_clients():
        return {
            LlmProvider.CUSTOM: CustomProviderClient(base_url="http://fake-vllm:8000", api_key=None)
        }

    monkeypatch.setattr("src.api.routes.llm_providers.default_clients", fake_default_clients)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "local-model-a"}]})

    _override_discovery_client(handler)
    try:
        await make_user("admin10@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin10@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/settings/llm-providers/custom/models", headers=_auth(token)
        )
        assert resp.status_code == 200
        assert resp.json()["models"] == [{"id": "local-model-a", "label": None}]
    finally:
        _clear_overrides()


async def test_models_discovery_connection_failure_is_502_not_an_unhandled_500(
    client, make_user, tmp_path, monkeypatch
):
    """The Docker-networking trap this test exists to catch: a base URL
    nothing is listening on must come back as a real 502 with a real
    message, never an unhandled 500 -- see
    test_agents_llm_router.py's matching test for OllamaClient/
    CustomProviderClient.complete() directly."""
    from src.agents.llm_router import OllamaClient

    _override_store(tmp_path, monkeypatch)

    def fake_default_clients():
        return {LlmProvider.OLLAMA: OllamaClient(base_url="http://fake-ollama:11434")}

    monkeypatch.setattr("src.api.routes.llm_providers.default_clients", fake_default_clients)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _override_discovery_client(handler)
    try:
        await make_user("admin12@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin12@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/settings/llm-providers/ollama/models", headers=_auth(token)
        )
        assert resp.status_code == 502
        assert "connection refused" in resp.json()["detail"].lower()
    finally:
        _clear_overrides()


async def test_models_discovery_custom_provider_without_base_url_is_502(
    client, make_user, tmp_path, monkeypatch
):
    from src.agents.llm_router import CustomProviderClient

    _override_store(tmp_path, monkeypatch)

    def fake_default_clients():
        return {LlmProvider.CUSTOM: CustomProviderClient(base_url="", api_key=None)}

    monkeypatch.setattr("src.api.routes.llm_providers.default_clients", fake_default_clients)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never call out with no base_url configured")

    _override_discovery_client(handler)
    try:
        await make_user("admin11@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin11@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/settings/llm-providers/custom/models", headers=_auth(token)
        )
        assert resp.status_code == 502
    finally:
        _clear_overrides()
