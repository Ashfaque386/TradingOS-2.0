"""In-app chat API tests (Build Spec §18: "streaming responses, abort,
per-session model switch, search, pin, export"). Available to every role
(no RBAC restriction), matching how strategies/backtests are shared
organizational state in this codebase.
"""

import json

import pytest
from httpx import AsyncClient

from src.core.redis_client import get_redis
from src.core.roles import Role
from src.main import app


@pytest.fixture(autouse=True)
def _isolate_redis_dependency(redis_client):
    """Same fix as test_webhooks_api.py: src.core.redis_client.get_redis()
    is a process-wide @lru_cache'd singleton, which collides with
    pytest-asyncio's per-test event loops once several tests in this file
    each make real Redis calls (the abort-flag check in
    stream_assistant_reply) -- override it to this test's own
    already-event-loop-scoped redis_client fixture instead."""
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_redis, None)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_token(client, make_user, email="op@example.com") -> str:
    await make_user(email, "supersecret1", Role.PORTFOLIO_MANAGER)
    return await _login(client, email, "supersecret1")


async def test_create_and_get_session(client, make_user):
    token = await _make_token(client, make_user)

    create_resp = await client.post(
        "/api/v1/chat/sessions",
        json={"title": "My session", "model": "anthropic"},
        headers=_auth(token),
    )
    assert create_resp.status_code == 201
    session = create_resp.json()
    assert session["title"] == "My session"
    assert session["model"] == "anthropic"
    assert session["pinned"] is False

    get_resp = await client.get(f"/api/v1/chat/sessions/{session['id']}", headers=_auth(token))
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == session["id"]


async def test_get_nonexistent_session_is_404(client, make_user):
    token = await _make_token(client, make_user)
    resp = await client.get(
        "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_pin_and_rename_via_patch(client, make_user):
    token = await _make_token(client, make_user)
    create_resp = await client.post(
        "/api/v1/chat/sessions", json={"title": "Untitled"}, headers=_auth(token)
    )
    session_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/chat/sessions/{session_id}",
        json={"title": "Renamed", "pinned": True},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["title"] == "Renamed"
    assert body["pinned"] is True


async def test_model_switch_then_clear_back_to_auto(client, make_user):
    token = await _make_token(client, make_user)
    create_resp = await client.post(
        "/api/v1/chat/sessions", json={"model": "anthropic"}, headers=_auth(token)
    )
    session_id = create_resp.json()["id"]

    switch_resp = await client.patch(
        f"/api/v1/chat/sessions/{session_id}", json={"model": "openai"}, headers=_auth(token)
    )
    assert switch_resp.json()["model"] == "openai"

    # Unrelated field update must not touch the model.
    rename_resp = await client.patch(
        f"/api/v1/chat/sessions/{session_id}", json={"title": "still openai"}, headers=_auth(token)
    )
    assert rename_resp.json()["model"] == "openai"

    clear_resp = await client.patch(
        f"/api/v1/chat/sessions/{session_id}", json={"clear_model": True}, headers=_auth(token)
    )
    assert clear_resp.json()["model"] is None


async def test_list_sessions_search_matches_title_and_message_content(client, make_user):
    token = await _make_token(client, make_user)
    await client.post(
        "/api/v1/chat/sessions", json={"title": "Kill switch discussion"}, headers=_auth(token)
    )
    other_resp = await client.post(
        "/api/v1/chat/sessions", json={"title": "Unrelated topic"}, headers=_auth(token)
    )
    other_id = other_resp.json()["id"]

    # Put a matching phrase into a message body, not the title, of the
    # "unrelated" session -- search must find it there too.
    await client.post(
        f"/api/v1/chat/sessions/{other_id}/messages",
        json={"content": "when did the kill switch last trip?"},
        headers=_auth(token),
    )

    search_resp = await client.get(
        "/api/v1/chat/sessions", params={"search": "kill switch"}, headers=_auth(token)
    )
    assert search_resp.status_code == 200
    titles = {s["title"] for s in search_resp.json()}
    assert titles == {"Kill switch discussion", "Unrelated topic"}


async def test_list_sessions_pinned_only_filter(client, make_user):
    token = await _make_token(client, make_user)
    pinned_resp = await client.post(
        "/api/v1/chat/sessions", json={"title": "Pinned one"}, headers=_auth(token)
    )
    await client.post("/api/v1/chat/sessions", json={"title": "Not pinned"}, headers=_auth(token))
    await client.patch(
        f"/api/v1/chat/sessions/{pinned_resp.json()['id']}",
        json={"pinned": True},
        headers=_auth(token),
    )

    resp = await client.get(
        "/api/v1/chat/sessions", params={"pinned_only": True}, headers=_auth(token)
    )
    titles = {s["title"] for s in resp.json()}
    assert titles == {"Pinned one"}


async def test_delete_session(client, make_user):
    token = await _make_token(client, make_user)
    create_resp = await client.post(
        "/api/v1/chat/sessions", json={"title": "To delete"}, headers=_auth(token)
    )
    session_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/chat/sessions/{session_id}", headers=_auth(token))
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=_auth(token))
    assert get_resp.status_code == 404


async def test_send_message_streams_the_no_provider_fallback_and_persists_it(client, make_user):
    """No real LLM provider is configured in this sandbox (same posture
    as every other LLM-touching test) -- the real fallback path fires,
    genuinely streamed over SSE, and the assistant reply is persisted."""
    token = await _make_token(client, make_user)
    create_resp = await client.post("/api/v1/chat/sessions", json={}, headers=_auth(token))
    session_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "what is my portfolio status?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["done"] is True
    full_text = "".join(e["text"] for e in events)
    assert "No LLM provider is currently configured" in full_text

    messages_resp = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=_auth(token)
    )
    messages = messages_resp.json()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "what is my portfolio status?"
    assert messages[1]["role"] == "assistant"
    assert "No LLM provider is currently configured" in messages[1]["content"]
    assert messages[1]["aborted"] is False


async def test_abort_endpoint_requires_an_existing_session(client, make_user):
    token = await _make_token(client, make_user)
    resp = await client.post(
        "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000/abort",
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_export_markdown_and_json(client, make_user):
    token = await _make_token(client, make_user)
    create_resp = await client.post(
        "/api/v1/chat/sessions", json={"title": "Export me"}, headers=_auth(token)
    )
    session_id = create_resp.json()["id"]
    await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "hello there"},
        headers=_auth(token),
    )

    md_resp = await client.get(
        f"/api/v1/chat/sessions/{session_id}/export",
        params={"format": "markdown"},
        headers=_auth(token),
    )
    assert md_resp.status_code == 200
    assert md_resp.headers["content-type"].startswith("text/markdown")
    assert "Export me" in md_resp.text
    assert "hello there" in md_resp.text

    json_resp = await client.get(
        f"/api/v1/chat/sessions/{session_id}/export",
        params={"format": "json"},
        headers=_auth(token),
    )
    assert json_resp.status_code == 200
    payload = json.loads(json_resp.text)
    assert payload["title"] == "Export me"
    assert payload["messages"][0]["content"] == "hello there"


async def test_export_unknown_format_is_400(client, make_user):
    token = await _make_token(client, make_user)
    create_resp = await client.post("/api/v1/chat/sessions", json={}, headers=_auth(token))
    session_id = create_resp.json()["id"]

    resp = await client.get(
        f"/api/v1/chat/sessions/{session_id}/export",
        params={"format": "pdf"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
