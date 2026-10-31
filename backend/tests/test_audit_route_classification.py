"""A guard for the no-op audit pattern: every mutating route must say what it does
when asked to do what is already true.

The audit middleware writes a row for every POST/PUT/PATCH/DELETE that answers 2xx.
A transition endpoint (reset, deactivate, activate, delete...) that answers 200 when
its target state already holds therefore puts a row in the permanent audit trail for
something that did not happen. That was found by reading, in two sweeps, and nothing
would have failed if a new endpoint reintroduced it. This test makes it fail.

Every mutating route in the API must appear in CLASSIFICATION below, in one of:

- NOT_A_TRANSITION       there is no target state that could already hold: it creates
                         something, computes, evaluates, triggers a job, or is inbound.
- SET_VALUE              a PUT-style save; the request itself is the action.
- REQUEST_ACCEPTED       a fire-and-forget request, or one that reports what it did in
                         the body (a count), so the row records a request that happened.
- REFUSES_NOOP           a transition that answers 404/409 when its target state already
                         holds (a refusal is never audited). Must name a test that proves
                         it, and the test must exist.
- IDEMPOTENT_SKIPS_AUDIT a transition documented to answer 200 on a repeat; its handler
                         must call `skip_audit_entry` so the repeat leaves no row.

A new endpoint fails `test_every_mutating_route_is_classified` until you classify it --
which means deciding, now, what it does on a repeat.
"""

import ast
import importlib
import inspect
import pkgutil
from collections.abc import Callable
from pathlib import Path

from fastapi.routing import APIRoute

import src.api.routes as routes_package
from src.main import app

API = "/api/v1"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
CATEGORIES = {
    "NOT_A_TRANSITION",
    "SET_VALUE",
    "REQUEST_ACCEPTED",
    "REFUSES_NOOP",
    "IDEMPOTENT_SKIPS_AUDIT",
}

SWEEP = "tests/test_noop_audit_sweep.py"
REFUSALS = "tests/test_noop_refusals.py"
N = "NOT_A_TRANSITION"
S = "SET_VALUE"
R = "REQUEST_ACCEPTED"
X = "REFUSES_NOOP"
I = "IDEMPOTENT_SKIPS_AUDIT"  # noqa: E741 - a short, local table key

# (method, path) -> (category, detail). For REFUSES_NOOP the detail is "<test file>::<test>".
CLASSIFICATION: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/admin/users"): (N, "creates a user"),
    ("PATCH", "/admin/users/{user_id}/role"): (
        X,
        f"{SWEEP}::test_admin_changes_that_change_nothing_are_refused_and_not_audited",
    ),
    ("POST", "/admin/users/{user_id}/deactivate"): (
        X,
        f"{SWEEP}::test_admin_changes_that_change_nothing_are_refused_and_not_audited",
    ),
    ("POST", "/admin/users/{user_id}/reactivate"): (
        X,
        f"{SWEEP}::test_admin_changes_that_change_nothing_are_refused_and_not_audited",
    ),
    ("POST", "/admin/users/{user_id}/revoke-sessions"): (
        R,
        "answers revoked_count; revoking is an incident-response step that ran",
    ),
    ("PUT", "/agents/{agent_id}/identity"): (S, "saves an identity"),
    ("POST", "/agents/{agent_id}/prompt-versions"): (N, "creates a draft version"),
    ("POST", "/agents/{agent_id}/prompt-versions/{version_id}/activate"): (
        X,
        f"{SWEEP}::test_reactivating_the_active_prompt_version_is_refused_and_changes_nothing",
    ),
    ("POST", "/agents/pipeline/run"): (N, "starts a run"),
    ("POST", "/agents/pipeline/runs/{run_id}/cancel"): (
        X,
        "tests/test_agents_pipeline_cancel.py::test_cancelling_an_unknown_or_finished_run_is_refused",
    ),
    ("POST", "/approvals/{approval_id}/decide"): (
        X,
        f"{REFUSALS}::test_deciding_an_approval_twice_is_refused_and_audited_once",
    ),
    ("POST", "/audit/verify"): (N, "compute-only: verifies the chain"),
    ("POST", "/auth/register"): (N, "creates an account"),
    ("POST", "/auth/login"): (N, "inbound authentication"),
    ("POST", "/auth/refresh"): (N, "inbound authentication"),
    ("POST", "/auth/logout"): (N, "inbound authentication"),
    ("POST", "/backtests"): (N, "runs and records a backtest"),
    ("POST", "/backtests/{run_id}/walk-forward"): (N, "compute-only"),
    ("POST", "/backtests/{run_id}/monte-carlo"): (N, "compute-only"),
    ("POST", "/backtests/compare"): (N, "compute-only"),
    ("POST", "/backtests/optimize"): (N, "runs and records an optimisation"),
    ("POST", "/broker-credentials/{broker}"): (S, "saves credentials"),
    ("DELETE", "/broker-credentials/{broker}"): (
        X,
        f"{SWEEP}::test_deleting_credentials_that_are_not_stored_is_a_404_and_not_audited",
    ),
    ("PUT", "/broker-credentials/{broker}/redirect-uri"): (S, "saves a redirect URL"),
    ("POST", "/chat/sessions"): (N, "creates a session"),
    ("PATCH", "/chat/sessions/{session_id}"): (S, "saves a title/model/pin"),
    ("DELETE", "/chat/sessions/{session_id}"): (
        X,
        f"{REFUSALS}::test_deleting_a_chat_session_twice_is_a_404_and_audited_once",
    ),
    ("POST", "/chat/sessions/{session_id}/messages"): (N, "adds a message"),
    ("POST", "/chat/sessions/{session_id}/abort"): (R, "sets a fire-and-forget abort flag"),
    ("PUT", "/gateway/config"): (S, "applies a config"),
    ("POST", "/gateway/config/validate"): (N, "compute-only"),
    ("POST", "/gateway/config/versions/{version_id}/rollback"): (
        N,
        "re-applies a version, recorded as a new applied version",
    ),
    ("POST", "/investor-reports/generate"): (N, "generates a report"),
    ("POST", "/kill-switch/{mode}/check"): (
        N,
        "evaluates a drawdown observation and reports the state",
    ),
    ("POST", "/kill-switch/{mode}/evaluate"): (
        N,
        "measures equity, records the reading and trips the switch at the limit (the trip "
        "writes its own audit row)",
    ),
    ("POST", "/kill-switch/{mode}/reset"): (
        X,
        f"{SWEEP}::test_resetting_a_kill_switch_that_is_not_tripped_is_refused_and_not_audited",
    ),
    ("POST", "/live-trading/subscriptions"): (N, "enrols a subscription"),
    ("POST", "/live-trading/daily-signal-run"): (
        N,
        "runs signal generation and reports the outcome",
    ),
    ("POST", "/live-trading/subscriptions/{subscription_id}/autonomy"): (
        I,
        "a repeat is a documented 200; the domain function writes no row either",
    ),
    ("POST", "/live-trading/subscriptions/{subscription_id}/generate-intent"): (
        N,
        "evaluates a tick and reports the intent or null",
    ),
    ("POST", "/settings/llm-providers/{provider}"): (S, "saves credentials"),
    ("DELETE", "/settings/llm-providers/{provider}"): (
        X,
        f"{SWEEP}::test_deleting_credentials_that_are_not_stored_is_a_404_and_not_audited",
    ),
    ("PUT", "/settings/llm-providers/{provider}/default-model"): (S, "saves a default model"),
    ("POST", "/settings/llm-providers/{provider}/test"): (N, "tests a connection"),
    ("POST", "/market-data/ingest/daily"): (N, "triggers a job"),
    ("POST", "/market-data/ingest/intraday"): (N, "triggers a job"),
    ("POST", "/market-data/ingest/corporate-actions"): (N, "triggers a job"),
    ("POST", "/market-data/ingest/instrument-master"): (N, "triggers a job"),
    ("POST", "/market-data/bhavcopy-fallback"): (N, "triggers a job"),
    ("POST", "/market-data/catalog-refresh"): (N, "triggers a job"),
    ("POST", "/market-data/backup"): (N, "triggers a job"),
    ("PUT", "/memory/settings"): (S, "saves the embedding choice"),
    ("DELETE", "/memory/settings"): (
        I,
        "a repeat reset is a documented 200 (tests/test_memory.py pins it)",
    ),
    ("POST", "/memory/{kind}"): (N, "remembers something"),
    ("DELETE", "/memory/{kind}/{memory_id}"): (
        X,
        "tests/test_memory.py::test_remember_query_and_forget_round_trip",
    ),
    ("POST", "/notification-channels/{channel}"): (S, "saves a channel"),
    ("PATCH", "/notification-channels/{channel}"): (S, "changes a channel non-secret settings"),
    ("DELETE", "/notification-channels/{channel}"): (
        X,
        f"{SWEEP}::test_deleting_credentials_that_are_not_stored_is_a_404_and_not_audited",
    ),
    ("POST", "/notification-channels/{channel}/test"): (N, "sends a test message"),
    ("POST", "/notification-channels/telegram/detect-chat-id"): (N, "queries Telegram"),
    ("POST", "/operator-guidance"): (N, "creates guidance"),
    ("POST", "/operator-guidance/{guidance_id}/deactivate"): (
        I,
        "a repeat is a documented 200; the domain function writes no row either",
    ),
    ("POST", "/orchestration/runs"): (N, "creates a run"),
    ("POST", "/orchestration/runs/{run_id}/pause"): (
        X,
        "tests/test_orchestration_run_control.py::test_a_control_that_would_change_nothing_is_refused_not_answered_200",
    ),
    ("POST", "/orchestration/runs/{run_id}/cancel"): (
        X,
        "tests/test_orchestration_run_cancel.py::test_cancel_over_http_is_409_for_a_finished_run_and_404_for_an_unknown_one",
    ),
    ("POST", "/orchestration/runs/{run_id}/continue"): (
        X,
        "tests/test_orchestration_run_control.py::test_a_control_that_would_change_nothing_is_refused_not_answered_200",
    ),
    ("POST", "/orchestration/runs/{run_id}/retry"): (
        X,
        "tests/test_orchestration_run_control.py::test_a_control_that_would_change_nothing_is_refused_not_answered_200",
    ),
    ("POST", "/orchestration/runs/{run_id}/rerun"): (N, "always creates a new run"),
    ("POST", "/paper-trading/subscriptions"): (N, "enrols a subscription"),
    ("POST", "/paper-trading/subscriptions/{subscription_id}/backfill-history"): (
        N,
        "fetches history and reports the outcome; repeating it is harmless (writes are idempotent)",
    ),
    ("POST", "/paper-trading/subscriptions/{subscription_id}/deactivate"): (
        X,
        "tests/test_paper_subscription_controls.py::test_deactivating_twice_is_refused_not_answered_200",
    ),
    ("POST", "/paper-trading/subscriptions/{subscription_id}/activate"): (
        X,
        "tests/test_paper_subscription_controls.py::test_activating_an_active_subscription_is_refused",
    ),
    ("POST", "/paper-trading/subscriptions/{subscription_id}/close-position"): (
        X,
        "tests/test_paper_subscription_controls.py::test_closing_a_flat_subscription_is_refused",
    ),
    ("PATCH", "/paper-trading/subscriptions/{subscription_id}/settings"): (
        X,
        "tests/test_paper_desk_previews.py::test_the_settings_route_refuses_a_no_op_a_short_reason_and_bad_values",
    ),
    ("POST", "/paper-trading/daily-signal-run"): (
        N,
        "runs signal generation and reports the outcome",
    ),
    ("POST", "/paper-trading/subscriptions/{subscription_id}/tick"): (
        N,
        "processes a tick and reports the fill or null",
    ),
    ("POST", "/portfolio/recommendations/generate"): (N, "generates a recommendation"),
    ("POST", "/portfolio/recommendations/{recommendation_id}/accept"): (
        X,
        "tests/test_portfolio_api.py::test_accept_an_already_decided_recommendation_is_404",
    ),
    ("POST", "/portfolio/recommendations/{recommendation_id}/reject"): (
        X,
        "tests/test_portfolio_api.py::test_accept_an_already_decided_recommendation_is_404",
    ),
    ("POST", "/risk/order-intents"): (N, "evaluates the order gate and reports the intent"),
    ("POST", "/risk/go-live-readiness"): (N, "compute-only"),
    ("POST", "/risk-limits/stage"): (N, "stages a change"),
    ("POST", "/risk-limits/{change_id}/confirm"): (
        X,
        f"{REFUSALS}::test_confirming_or_applying_a_risk_limit_change_twice_is_refused_and_audited_once",
    ),
    ("POST", "/risk-limits/{change_id}/reject"): (
        X,
        f"{REFUSALS}::test_rejecting_a_risk_limit_change_twice_is_refused_and_audited_once",
    ),
    ("POST", "/risk-limits/{change_id}/apply"): (
        X,
        f"{REFUSALS}::test_confirming_or_applying_a_risk_limit_change_twice_is_refused_and_audited_once",
    ),
    ("POST", "/shadow-mode/check"): (N, "runs a check and records it"),
    ("POST", "/strategies"): (N, "creates a strategy"),
    ("POST", "/strategies/{strategy_id}/suggestions"): (N, "creates a suggestion"),
    ("POST", "/strategies/{strategy_id}/suggestions/{suggestion_id}/review"): (
        N,
        "runs a review and reports it",
    ),
    ("POST", "/strategies/{strategy_id}/suggestions/{suggestion_id}/regenerate"): (
        N,
        "creates a regenerated version",
    ),
    ("POST", "/strategies/{strategy_id}/request-promotion"): (N, "creates an approval request"),
    ("POST", "/strategies/{strategy_id}/promote"): (
        X,
        f"{REFUSALS}::test_promoting_a_strategy_twice_is_refused_and_audited_once",
    ),
    ("POST", "/strategies/{strategy_id}/request-live-eligibility"): (
        N,
        "creates an approval request",
    ),
    ("POST", "/strategies/{strategy_id}/approve-live-eligibility"): (
        X,
        f"{REFUSALS}::test_approving_live_eligibility_for_a_strategy_not_in_paper_trading_is_refused",
    ),
    ("POST", "/system/scheduled-jobs/{scheduler_name}/{job_id}/run-now"): (N, "triggers a job"),
    ("PUT", "/system/scheduled-jobs/{scheduler_name}/{job_id}/schedule"): (S, "saves a schedule"),
    ("POST", "/system/jwt-signing-key/rotate"): (N, "always rotates"),
    ("POST", "/webhooks/telegram"): (N, "inbound webhook"),
    ("POST", "/webhooks/discord"): (N, "inbound webhook"),
    ("POST", "/webhooks/slack"): (N, "inbound webhook"),
}


def _served_routes() -> set[tuple[str, str]]:
    """Every mutating route the running app serves, from its public OpenAPI schema."""
    served = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method.upper() in MUTATING:
                assert path.startswith(API), path
                served.add((method.upper(), path[len(API) :]))
    return served


def _endpoints() -> dict[tuple[str, str], Callable[..., object]]:
    """(method, path) -> handler, from each route module's own router."""
    found: dict[tuple[str, str], Callable[..., object]] = {}
    for module_info in pkgutil.iter_modules(routes_package.__path__):
        module = importlib.import_module(f"src.api.routes.{module_info.name}")
        router = getattr(module, "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                for method in route.methods & MUTATING:
                    found[(method, route.path)] = route.endpoint
    return found


def test_every_mutating_route_is_classified():
    unclassified = sorted(_served_routes() - set(CLASSIFICATION))

    assert not unclassified, (
        "These mutating routes are not in CLASSIFICATION "
        "(tests/test_audit_route_classification.py):\n"
        + "\n".join(f"  {m} {p}" for m, p in unclassified)
        + "\nThe audit middleware writes a row for every 2xx. Decide what each does when asked to"
        " do what is already true, and classify it -- see this module's docstring."
    )


def test_no_classification_is_stale():
    stale = sorted(set(CLASSIFICATION) - _served_routes())

    assert not stale, f"classified, but no longer served: {stale}"


def test_every_classification_uses_a_known_category():
    assert all(category in CATEGORIES for category, _ in CLASSIFICATION.values())


def test_every_served_route_can_be_traced_to_its_handler():
    """So the source checks below cannot silently skip a route."""
    missing = sorted(_served_routes() - set(_endpoints()))

    assert not missing, f"served, but no module router has a handler for: {missing}"


def _function_exists(reference: str) -> bool:
    file_name, _, test_name = reference.partition("::")
    path = Path(__file__).resolve().parents[1] / file_name
    if not path.is_file():
        return False
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == test_name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


def test_every_refusal_names_a_test_that_exists():
    broken = sorted(
        f"{m} {p} -> {detail}"
        for (m, p), (category, detail) in CLASSIFICATION.items()
        if category == "REFUSES_NOOP" and not _function_exists(detail)
    )

    assert not broken, "REFUSES_NOOP routes whose named test does not exist:\n" + "\n".join(broken)


def test_idempotent_handlers_call_skip_audit_entry_and_only_they_do():
    endpoints = _endpoints()
    declared = {
        key for key, (category, _) in CLASSIFICATION.items() if category == "IDEMPOTENT_SKIPS_AUDIT"
    }

    skips = {key for key, fn in endpoints.items() if "skip_audit_entry(" in inspect.getsource(fn)}

    assert (
        declared - skips == set()
    ), f"declared idempotent, but never skips the audit row: {declared - skips}"
    assert (
        skips - declared == set()
    ), f"skips the audit row without being declared idempotent: {skips - declared}"
