"""LLM router tests (Build Spec §7.1 requirement 1): multi-provider
fallback, per-provider failure tracking, and live re-reads of the Gateway
config's fallback order with no restart needed. Uses injected fake clients
(same pattern as orchestration/planner.py's fake_llm_planner) -- this
sandbox has no LLM provider keys and blocked egress, so the real httpx
clients are never exercised here.
"""

import pytest

from src.agents.llm_router import (
    LlmCompletionPayload,
    LlmProviderError,
    LlmRouter,
    LlmRouterExhaustedError,
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
