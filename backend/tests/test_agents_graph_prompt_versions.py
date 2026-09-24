"""Phase 19 (docs/phase19-audit.md Part 2.2): proves activating a prompt
version actually changes the prompt text sent to the LLM -- the audit's
central finding was that `prompt_versions` used to have zero effect on
real LLM calls.
"""

from src.agents import graph as graph_module
from src.agents.graph import _with_active_prompt, run_pipeline
from src.agents.state import TradingOSGraphState


def test_with_active_prompt_prefixes_when_set():
    state = TradingOSGraphState(objective="obj", active_prompts={"ceo-agent": "SPECIAL PERSONA"})
    result = _with_active_prompt(state, "ceo-agent", "task instruction")
    assert result == "SPECIAL PERSONA\n\ntask instruction"


def test_with_active_prompt_is_unchanged_when_no_active_prompt_exists():
    state = TradingOSGraphState(objective="obj")
    result = _with_active_prompt(state, "ceo-agent", "task instruction")
    assert result == "task instruction"


async def test_run_pipeline_passes_active_prompt_into_the_real_ceo_kickoff_call(monkeypatch):
    captured_prompts = []

    async def _spy(router, agent_id, prompt, fallback):
        captured_prompts.append((agent_id, prompt))
        return {"source": "fallback", **fallback}

    monkeypatch.setattr(graph_module, "_llm_or_fallback", _spy)

    await run_pipeline(
        "Build a momentum strategy", active_prompts={"ceo-agent": "ACTIVATED PROMPT"}
    )

    ceo_call = next(p for agent_id, p in captured_prompts if agent_id == "ceo-agent")
    assert "ACTIVATED PROMPT" in ceo_call
    assert "Build a momentum strategy" in ceo_call


async def test_run_pipeline_with_no_active_prompts_behaves_exactly_as_before(monkeypatch):
    captured_prompts = []

    async def _spy(router, agent_id, prompt, fallback):
        captured_prompts.append((agent_id, prompt))
        return {"source": "fallback", **fallback}

    monkeypatch.setattr(graph_module, "_llm_or_fallback", _spy)

    await run_pipeline("Build a momentum strategy")

    ceo_call = next(p for agent_id, p in captured_prompts if agent_id == "ceo-agent")
    assert ceo_call == "Kick off a trading strategy research objective: Build a momentum strategy"
