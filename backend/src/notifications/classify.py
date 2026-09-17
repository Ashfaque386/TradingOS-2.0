"""Message classification for inbound routing (Build Spec §18: "Route
verified-sender messages to the CEO Agent, with the option to spawn an
organization run via message classification").

A deterministic keyword heuristic, not an LLM classifier -- this sandbox
has no LLM provider keys/egress (the same constraint documented across
every phase touching src.agents.llm_router), and a classification step
that silently degrades to "always query" whenever providers are
unavailable would make the *feature itself* untestable offline. The
heuristic is real, not a stub: an operator's genuine command message
("research NIFTY momentum strategies", "run backtest on RELIANCE") reads
as an objective and spawns a real organization run; anything else is
treated as a conversational query and gets a direct CEO Agent reply
instead. `classify_message` is a free function specifically so it can be
swapped for a real LLM-based classifier later with no caller changes --
the same "real-but-simple now, swappable later" posture as every
Fake-prefixed provider elsewhere in this codebase.
"""

from typing import Literal

ClassificationResult = Literal["objective", "query"]

# Imperative verbs that, as the first word of a message, mark it as a
# command to act rather than a question to answer. Deliberately small and
# reviewable -- Build Spec §16 refuses "open marketplace ingestion" for
# skills, and an unreviewable classifier keyword list would be the same
# kind of ungoverned surface for message routing.
_OBJECTIVE_LEAD_VERBS = frozenset(
    {
        "research",
        "analyze",
        "analyse",
        "backtest",
        "find",
        "run",
        "investigate",
        "generate",
        "build",
        "check",
        "evaluate",
        "optimize",
        "optimise",
        "monitor",
        "deploy",
        "start",
        "create",
    }
)


def classify_message(text: str) -> ClassificationResult:
    stripped = text.strip()
    if not stripped:
        return "query"
    if stripped.endswith("?"):
        return "query"
    first_word = stripped.split(maxsplit=1)[0].lower().strip(".,!:;")
    if first_word in _OBJECTIVE_LEAD_VERBS:
        return "objective"
    return "query"
