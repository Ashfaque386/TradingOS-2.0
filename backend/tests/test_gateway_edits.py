"""What a console edit does to the gateway config's data: one agent's settings, the LLM order."""

import copy
from typing import Any

import pytest

from src.gateway.edits import (
    AgentSettingsPatch,
    InvalidEdit,
    NothingToChange,
    UnknownAgent,
    apply_agent_settings,
    apply_provider_order,
    effective_settings,
)

KNOWN = {"market-data-read", "notification-send", "code-format-lint"}


def config() -> dict[str, Any]:
    return {
        "infra": {"llmProviders": {"order": ["anthropic", "openai"]}},
        "agents": {
            "defaults": {
                "model": "auto",
                "heartbeatEnabled": False,
                "skills": ["market-data-read", "notification-send"],
            },
            "entries": {
                "ceo-agent": {"heartbeatEnabled": True, "heartbeatIntervalMinutes": 10},
                "risk-manager": {"skills": ["market-data-read"]},
            },
        },
    }


def test_an_agent_with_no_entry_is_on_with_the_defaults():
    assert effective_settings(config(), "market-analyst") == {
        "enabled": True,
        "heartbeat_enabled": False,
        "heartbeat_interval_minutes": None,
        "skills": ["market-data-read", "notification-send"],
    }


def test_an_entry_overrides_the_defaults_field_by_field():
    assert effective_settings(config(), "ceo-agent") == {
        "enabled": True,
        "heartbeat_enabled": True,
        "heartbeat_interval_minutes": 10,
        "skills": ["market-data-read", "notification-send"],
    }
    assert effective_settings(config(), "risk-manager")["skills"] == ["market-data-read"]


def test_an_agent_that_is_not_in_the_roster_is_unknown():
    with pytest.raises(UnknownAgent, match="not-an-agent"):
        apply_agent_settings(
            config(), "not-an-agent", AgentSettingsPatch(enabled=False), known_skills=KNOWN
        )


def test_a_patch_that_asks_for_nothing_is_refused():
    with pytest.raises(NothingToChange, match="Nothing was asked to change"):
        apply_agent_settings(config(), "ceo-agent", AgentSettingsPatch(), known_skills=KNOWN)


def test_switching_an_agent_off_is_one_change_and_recorded_in_its_entry():
    data = config()
    changed = apply_agent_settings(
        data, "market-analyst", AgentSettingsPatch(enabled=False), known_skills=KNOWN
    )
    assert changed == ["enabled"]
    assert data["agents"]["entries"]["market-analyst"] == {"enabled": False}
    assert effective_settings(data, "market-analyst")["enabled"] is False


def test_switching_on_an_agent_that_is_on_changes_nothing():
    with pytest.raises(NothingToChange, match="already so"):
        apply_agent_settings(
            config(), "market-analyst", AgentSettingsPatch(enabled=True), known_skills=KNOWN
        )


def test_the_audit_agent_cannot_be_switched_off_and_the_message_says_why():
    with pytest.raises(InvalidEdit, match="Audit Agent cannot be switched off"):
        apply_agent_settings(
            config(), "audit-agent", AgentSettingsPatch(enabled=False), known_skills=KNOWN
        )


def test_the_audit_agent_can_have_its_other_settings_changed():
    data = config()
    assert apply_agent_settings(
        data, "audit-agent", AgentSettingsPatch(heartbeat_enabled=True), known_skills=KNOWN
    ) == ["heartbeat_enabled"]


def test_turning_a_heartbeat_on_with_an_interval_changes_both():
    data = config()
    changed = apply_agent_settings(
        data,
        "market-analyst",
        AgentSettingsPatch(heartbeat_enabled=True, heartbeat_interval_minutes=15),
        known_skills=KNOWN,
    )
    assert changed == ["heartbeat_enabled", "heartbeat_interval_minutes"]
    assert data["agents"]["entries"]["market-analyst"] == {
        "heartbeatEnabled": True,
        "heartbeatIntervalMinutes": 15,
    }


def test_turning_a_heartbeat_off_forgets_its_interval_unless_one_is_given():
    data = config()
    changed = apply_agent_settings(
        data, "ceo-agent", AgentSettingsPatch(heartbeat_enabled=False), known_skills=KNOWN
    )
    assert changed == ["heartbeat_enabled", "heartbeat_interval_minutes"]
    assert "heartbeatIntervalMinutes" not in data["agents"]["entries"]["ceo-agent"]

    kept = config()
    apply_agent_settings(
        kept,
        "ceo-agent",
        AgentSettingsPatch(heartbeat_enabled=False, heartbeat_interval_minutes=20),
        known_skills=KNOWN,
    )
    assert kept["agents"]["entries"]["ceo-agent"]["heartbeatIntervalMinutes"] == 20


def test_a_heartbeat_interval_must_be_at_least_a_minute():
    with pytest.raises(InvalidEdit, match="at least one minute"):
        apply_agent_settings(
            config(),
            "ceo-agent",
            AgentSettingsPatch(heartbeat_interval_minutes=0),
            known_skills=KNOWN,
        )


def test_skills_are_granted_from_the_shipped_set_in_the_order_given_without_repeats():
    data = config()
    changed = apply_agent_settings(
        data,
        "risk-manager",
        AgentSettingsPatch(skills=("market-data-read", "code-format-lint", "code-format-lint")),
        known_skills=KNOWN,
    )
    assert changed == ["skills"]
    assert data["agents"]["entries"]["risk-manager"]["skills"] == [
        "market-data-read",
        "code-format-lint",
    ]


def test_a_skill_that_is_not_shipped_is_refused_by_name_and_nothing_changes():
    data = config()
    before = copy.deepcopy(data)
    with pytest.raises(InvalidEdit, match="No such skill: made-up, typo"):
        apply_agent_settings(
            data,
            "risk-manager",
            AgentSettingsPatch(skills=("made-up", "market-data-read", "typo")),
            known_skills=KNOWN,
        )
    assert data == before


def test_granting_exactly_what_an_agent_already_has_changes_nothing_even_in_another_order():
    with pytest.raises(NothingToChange):
        apply_agent_settings(
            config(),
            "market-analyst",
            AgentSettingsPatch(skills=("notification-send", "market-data-read")),
            known_skills=KNOWN,
        )


def test_removing_every_skill_is_a_change():
    data = config()
    assert apply_agent_settings(
        data, "risk-manager", AgentSettingsPatch(skills=()), known_skills=KNOWN
    ) == ["skills"]
    assert effective_settings(data, "risk-manager")["skills"] == []


def test_the_provider_order_is_replaced_and_only_that():
    data = config()
    apply_provider_order(
        data, ["openai", "anthropic", "gemini"], known_providers={"anthropic", "openai", "gemini"}
    )
    assert data["infra"]["llmProviders"]["order"] == ["openai", "anthropic", "gemini"]
    assert data["agents"] == config()["agents"]


def test_the_provider_order_refuses_an_empty_chain_a_stranger_and_a_repeat():
    known = {"anthropic", "openai"}
    with pytest.raises(InvalidEdit, match="at least one provider"):
        apply_provider_order(config(), [], known_providers=known)
    with pytest.raises(InvalidEdit, match="No such LLM provider: mystery"):
        apply_provider_order(config(), ["anthropic", "mystery"], known_providers=known)
    with pytest.raises(InvalidEdit, match="only once"):
        apply_provider_order(config(), ["anthropic", "anthropic"], known_providers=known)


def test_the_provider_order_already_in_place_is_nothing_to_change():
    with pytest.raises(NothingToChange, match="already in that order"):
        apply_provider_order(
            config(), ["anthropic", "openai"], known_providers={"anthropic", "openai"}
        )
