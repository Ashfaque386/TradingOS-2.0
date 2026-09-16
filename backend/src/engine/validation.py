"""Static validation for LLM-generated strategy code (Build Spec §9): an
AST ban-list, then a real `ruff` lint pass, both run BEFORE anything
reaches the sandbox (src/engine/sandbox/) -- this is the cheap, fast gate
that rejects categorically-dangerous code without ever spending a sandbox
worker on it.

This is a stricter, strategy-pipeline-specific gate than
src/agents/tools/code_lint.py's general-purpose Skill Registry tool (banned
imports only, Phase 3): it additionally bans eval/exec/dynamic-import
calls and a broader import allowlist, and runs the repo's real `ruff`
binary via subprocess rather than only an AST walk. The two modules
deliberately don't share code -- they gate different things for different
callers (an arbitrary agent skill call vs. a strategy destined for the
sandbox) and conflating them would make either one's ban-list harder to
reason about in isolation.
"""

import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Build Spec §9: "no os, subprocess, socket, eval, exec, dynamic imports
# outside an allowlist". sys/shutil/ctypes/importlib/pty/multiprocessing
# added for the same reason os/subprocess are banned -- each is another
# way to touch the host process/filesystem/other-processes outside the
# `run_backtest(data, config) -> dict` contract's legitimate surface.
BANNED_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "socket",
        "sys",
        "shutil",
        "ctypes",
        "importlib",
        "pty",
        "multiprocessing",
        "threading",
        "signal",
    }
)

# Everything a run_backtest implementation should plausibly need. Anything
# not listed here AND not in BANNED_IMPORTS is still rejected -- "dynamic
# imports outside an allowlist" reads as "only these names may be
# imported", not merely "these specific names are banned".
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "pandas",
        "numpy",
        "math",
        "statistics",
        "datetime",
        "decimal",
        "json",
        "itertools",
        "functools",
        "collections",
        "typing",
        "dataclasses",
        "re",
    }
)

# eval/exec/compile/__import__ are the standard dynamic-code-execution
# surface; banning them as *calls* (not just imports) matters because
# they're builtins, not things that need to be imported first.
BANNED_CALL_NAMES: frozenset[str] = frozenset({"eval", "exec", "compile", "__import__"})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)


def _call_target_name(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def check_ast(code: str) -> list[str]:
    """The ban-list gate. Never raises for invalid Python -- a SyntaxError
    is itself a validation failure, reported the same way as any other.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc}"]

    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    errors.append(f"banned import: {alias.name}")
                elif root not in ALLOWED_IMPORTS:
                    errors.append(f"import not in allowlist: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                errors.append(f"banned import: {node.module}")
            elif root and root not in ALLOWED_IMPORTS:
                errors.append(f"import not in allowlist: {node.module}")
        elif isinstance(node, ast.Call):
            name = _call_target_name(node)
            if name in BANNED_CALL_NAMES:
                errors.append(f"banned call: {name}()")
    return errors


def check_ruff(code: str) -> list[str]:
    """Real `ruff check`, not a stub -- run against a temp file since ruff
    operates on files, not strings. Only called after check_ast() passes,
    so this never has to make sense of syntactically-broken or
    already-rejected code.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = Path(f.name)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--quiet", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()] or [
            f"ruff check failed (exit {result.returncode}): {result.stderr.strip()}"
        ]
    finally:
        path.unlink(missing_ok=True)


def validate_strategy_code(code: str) -> ValidationResult:
    errors = check_ast(code)
    if not errors:
        # Only lint code that already passed the ban-list -- running ruff
        # over banned/syntactically-broken code would just add noise on
        # top of an already-failing result.
        errors = check_ruff(code)
    return ValidationResult(passed=not errors, errors=errors)
