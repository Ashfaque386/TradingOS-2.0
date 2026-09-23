"""LLM router tests (Build Spec §7.1 requirement 1): multi-provider
fallback, per-provider failure tracking, and live re-reads of the Gateway
config's fallback order with no restart needed. Uses injected fake clients
(same pattern as orchestration/planner.py's fake_llm_planner) -- this
sandbox has no LLM provider keys and blocked egress, so the real httpx
clients are never exercised here.
"""

from datetime import datetime

import pytest

from src.agents.llm_router import (
    AnthropicClient,
    CustomProviderClient,
    LlmCompletionPayload,
    LlmProviderError,
    LlmRouter,
    LlmRouterExhaustedError,
    OllamaClient,
    llm_provider_health_vitals,
)
from src.gateway.apply import apply_config_text
from src.gateway.schema import LlmProvider

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


class _AlwaysFails:
    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
        raise LlmProviderError("simulated provider failure")


class _AlwaysSucceeds:
    def __init__(
        self,
        text: str = "canned completion",
        *,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    async def complete(self, *, model: str, prompt: str) -> LlmCompletionPayload:
        return LlmCompletionPayload(
            text=self.text,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


async def test_router_falls_back_to_next_provider_on_failure(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai", "gemini"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysFails(),
            LlmProvider.OPENAI: _AlwaysFails(),
            LlmProvider.GEMINI: _AlwaysSucceeds(),
        }
    )
    result = await router.complete(agent_id="ceo-agent", prompt="hi")

    assert result.provider == LlmProvider.GEMINI
    assert result.used_fallback is True
    assert result.text == "canned completion"
    assert result.failed_providers == (LlmProvider.ANTHROPIC, LlmProvider.OPENAI)

    assert router.health_for(LlmProvider.ANTHROPIC).last_failure_at is not None
    assert router.health_for(LlmProvider.OPENAI).last_failure_at is not None
    assert router.health_for(LlmProvider.GEMINI).last_success_at is not None
    assert router.health_for(LlmProvider.GEMINI).served_as_fallback is True


async def test_router_does_not_use_fallback_when_first_provider_succeeds(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={LlmProvider.ANTHROPIC: _AlwaysSucceeds(), LlmProvider.OPENAI: _AlwaysFails()}
    )
    result = await router.complete(agent_id="ceo-agent", prompt="hi")

    assert result.provider == LlmProvider.ANTHROPIC
    assert result.used_fallback is False
    assert router.health_for(LlmProvider.OPENAI).last_failure_at is None


async def test_router_exhausted_raises_with_every_providers_error(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={LlmProvider.ANTHROPIC: _AlwaysFails(), LlmProvider.OPENAI: _AlwaysFails()}
    )
    with pytest.raises(LlmRouterExhaustedError) as exc_info:
        await router.complete(agent_id="ceo-agent", prompt="hi")

    assert len(exc_info.value.errors) == 2
    assert exc_info.value.agent_id == "ceo-agent"


async def test_reordering_gateway_config_takes_effect_without_recreating_router(
    db_session_factory,
):
    """Requirement 1: "no restart needed to change [fallback] order" --
    the same LlmRouter instance must pick up a hot-reloaded config change
    on its very next call.
    """
    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysSucceeds("a"),
            LlmProvider.OPENAI: _AlwaysSucceeds("b"),
        }
    )

    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")
    result1 = await router.complete(agent_id="ceo-agent", prompt="hi")
    assert result1.provider == LlmProvider.ANTHROPIC

    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("openai", "anthropic"), source="t")
    result2 = await router.complete(agent_id="ceo-agent", prompt="hi")
    assert result2.provider == LlmProvider.OPENAI


# --- Phase 12 (Build Spec §18): preferred_provider + stream_complete -------


async def test_preferred_provider_is_tried_first_ahead_of_configured_order(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysSucceeds("from anthropic"),
            LlmProvider.OPENAI: _AlwaysSucceeds("from openai"),
        }
    )
    result = await router.complete(
        agent_id="ceo-agent", prompt="hi", preferred_provider=LlmProvider.OPENAI
    )

    assert result.provider == LlmProvider.OPENAI
    assert result.text == "from openai"


async def test_preferred_provider_still_falls_back_to_the_rest_of_the_chain_on_failure(
    db_session_factory,
):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysSucceeds("from anthropic"),
            LlmProvider.OPENAI: _AlwaysFails(),
        }
    )
    result = await router.complete(
        agent_id="ceo-agent", prompt="hi", preferred_provider=LlmProvider.OPENAI
    )

    assert (
        result.provider == LlmProvider.ANTHROPIC
    ), "preferred provider failing must still fall back"


async def test_stream_complete_yields_word_chunks_then_a_final_done_chunk(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("openai"), source="t")

    router = LlmRouter(clients={LlmProvider.OPENAI: _AlwaysSucceeds("hello brave world")})

    chunks = [c async for c in router.stream_complete(agent_id="ceo-agent", prompt="hi")]

    assert chunks[-1].done is True
    assert chunks[-1].provider == LlmProvider.OPENAI
    assert "".join(c.text for c in chunks) == "hello brave world"
    assert all(not c.done for c in chunks[:-1])


async def test_stream_complete_falls_back_before_any_chunk_is_yielded(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysFails(),
            LlmProvider.OPENAI: _AlwaysSucceeds("fallback text"),
        }
    )

    chunks = [c async for c in router.stream_complete(agent_id="ceo-agent", prompt="hi")]

    assert chunks[-1].provider == LlmProvider.OPENAI
    assert "".join(c.text for c in chunks) == "fallback text"


async def test_stream_complete_exhausted_raises(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={LlmProvider.ANTHROPIC: _AlwaysFails(), LlmProvider.OPENAI: _AlwaysFails()}
    )

    with pytest.raises(LlmRouterExhaustedError):
        async for _ in router.stream_complete(agent_id="ceo-agent", prompt="hi"):
            pass


async def test_stream_complete_real_anthropic_client_with_no_key_falls_back_before_yielding(
    db_session_factory,
):
    """`_stream_anthropic` (the genuine Anthropic SSE-streaming code
    path, not a fake) checks for an API key before opening any
    connection -- this sandbox has no real key/egress (same posture as
    every other real provider client in this module), so this exercises
    that real early-exit path and confirms it falls back cleanly."""
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: AnthropicClient(api_key=None),
            LlmProvider.OPENAI: _AlwaysSucceeds("real fallback"),
        }
    )

    chunks = [c async for c in router.stream_complete(agent_id="ceo-agent", prompt="hi")]

    assert chunks[-1].provider == LlmProvider.OPENAI
    assert "".join(c.text for c in chunks) == "real fallback"


async def test_ollama_client_reports_a_graceful_error_on_connection_failure():
    """The Docker-networking trap this test exists to catch: a bare
    `localhost`/loopback base URL that nothing is listening on (exactly
    what `http://localhost:11434` resolves to from inside the backend's
    own container when Ollama actually runs on the host) must surface as
    a real LlmProviderError, never an unhandled httpx exception -- a
    genuine connection attempt against an unreachable loopback port,
    same "real attempt" posture as this codebase's other network tests,
    no mock transport needed since loopback traffic never goes through
    this sandbox's egress proxy."""
    client = OllamaClient(base_url="http://127.0.0.1:1")
    with pytest.raises(LlmProviderError, match="ollama: connection failed"):
        await client.complete(model="llama3", prompt="hi")


async def test_custom_provider_client_reports_a_graceful_error_on_connection_failure():
    client = CustomProviderClient(base_url="http://127.0.0.1:1")
    with pytest.raises(LlmProviderError, match="custom: connection failed"):
        await client.complete(model="local-model", prompt="hi")


async def test_llm_provider_health_vitals_reports_the_real_singleton_routers_state(
    db_session_factory, monkeypatch
):
    """GET /api/v1/system/vitals's `llm.provider_health` field -- reads
    the same process-wide `get_llm_router()` singleton every real caller
    (orchestration/strategies.py, chat.py, notifications/inbound_router.py)
    already updates, not a fresh, always-empty router."""
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic", "openai"), source="t")

    router = LlmRouter(
        clients={
            LlmProvider.ANTHROPIC: _AlwaysFails(),
            LlmProvider.OPENAI: _AlwaysSucceeds("hi"),
        }
    )
    monkeypatch.setattr("src.agents.llm_router._router", router)

    await router.complete(agent_id="ceo-agent", prompt="hi")

    health = llm_provider_health_vitals()
    assert [entry["provider"] for entry in health] == ["anthropic", "openai"]

    anthropic_entry, openai_entry = health
    assert anthropic_entry["last_success_at"] is None
    assert anthropic_entry["served_as_fallback"] is False
    # ISO-8601 with timezone, never a raw epoch float.
    datetime.fromisoformat(anthropic_entry["last_failure_at"])

    assert openai_entry["last_failure_at"] is None
    assert openai_entry["served_as_fallback"] is True
    datetime.fromisoformat(openai_entry["last_success_at"])


async def test_llm_provider_health_vitals_reports_an_unused_provider_as_honestly_untouched(
    db_session_factory, monkeypatch
):
    async with db_session_factory() as db:
        await apply_config_text(db, _config_with_order("anthropic"), source="t")

    router = LlmRouter(clients={LlmProvider.ANTHROPIC: _AlwaysSucceeds("hi")})
    monkeypatch.setattr("src.agents.llm_router._router", router)

    health = llm_provider_health_vitals()
    assert health == [
        {
            "provider": "anthropic",
            "last_failure_at": None,
            "last_success_at": None,
            "served_as_fallback": False,
        }
    ]
