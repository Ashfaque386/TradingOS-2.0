"""Static validation tests (Build Spec §9): the AST ban-list rejects
banned imports and calls, an import outside the allowlist is rejected too,
and a real ruff lint pass catches style/correctness issues in code that
otherwise passes the ban-list.
"""

from src.engine.validation import validate_strategy_code


def test_valid_code_passes():
    code = """
def run_backtest(data, config):
    total = sum(1 for _ in range(10))
    return {"trades": total}
"""
    result = validate_strategy_code(code)
    assert result.passed is True
    assert result.errors == []


def test_banned_import_os_is_rejected():
    code = """
import os
def run_backtest(data, config):
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("os" in e for e in result.errors)


def test_banned_import_subprocess_is_rejected():
    code = """
import subprocess
def run_backtest(data, config):
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("subprocess" in e for e in result.errors)


def test_banned_import_socket_is_rejected():
    code = """
from socket import socket
def run_backtest(data, config):
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("socket" in e for e in result.errors)


def test_import_not_in_allowlist_is_rejected():
    code = """
import requests
def run_backtest(data, config):
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("not in allowlist" in e for e in result.errors)


def test_allowlisted_import_passes_ast_gate():
    code = """
import math


def run_backtest(data, config):
    return {"pi": math.pi}
"""
    result = validate_strategy_code(code)
    assert result.passed is True, result.errors


def test_eval_call_is_rejected():
    code = """
def run_backtest(data, config):
    eval("1+1")
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("eval" in e for e in result.errors)


def test_exec_call_is_rejected():
    code = """
def run_backtest(data, config):
    exec("x=1")
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("exec" in e for e in result.errors)


def test_dynamic_import_call_is_rejected():
    code = """
def run_backtest(data, config):
    __import__("os")
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("__import__" in e for e in result.errors)


def test_syntax_error_is_reported_not_raised():
    code = "def run_backtest(data, config:\n    return"
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("SyntaxError" in e for e in result.errors)


def test_real_ruff_lint_catches_issues_ast_ban_list_does_not():
    # Structurally fine (no banned imports/calls), but a real style/
    # correctness issue a bare AST ban-list walk would never catch.
    code = """
def run_backtest(data, config):
    unused_variable = 42
    return {}
"""
    result = validate_strategy_code(code)
    assert result.passed is False
    assert any("F841" in e for e in result.errors)
