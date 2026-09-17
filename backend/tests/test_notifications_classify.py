"""Message classification tests (Build Spec §18's "spawn an organization
run via message classification").
"""

from src.notifications.classify import classify_message


def test_imperative_lead_verb_classifies_as_objective():
    assert classify_message("research NIFTY momentum strategies") == "objective"


def test_run_backtest_classifies_as_objective():
    assert classify_message("Run backtest on RELIANCE with SMA crossover") == "objective"


def test_question_classifies_as_query():
    assert classify_message("What is the current kill switch state?") == "query"


def test_non_imperative_statement_classifies_as_query():
    assert classify_message("thanks for the update") == "query"


def test_empty_message_classifies_as_query():
    assert classify_message("   ") == "query"


def test_classification_is_case_insensitive_on_the_lead_verb():
    assert classify_message("BACKTEST the momentum strategy") == "objective"
