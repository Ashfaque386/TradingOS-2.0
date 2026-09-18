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
import re
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
#
# getattr/setattr/delattr/vars/globals/locals are banned for a different
# reason: reflection, not code execution. Confirmed live (adversarial
# testing against the real sandbox worker) that `run_backtest` code with
# NO banned import and NO banned call from the set above can still fully
# escape restricted_exec.py's guards -- `__import__.__globals__["_real_import"]`
# recovers the unrestricted `__import__`, and `open.__closure__[i].cell_contents`
# recovers the unrestricted `open`, both via ordinary dot-attribute access
# on ordinary Python objects, no import or eval/exec needed. Banning dunder
# *attribute access* below (BANNED_ATTRIBUTE_PATTERN) closes the dot-notation
# form of this; banning these calls closes the `getattr(open, "__closure__")`
# form that would otherwise dodge the attribute-node scan via a string
# literal (or a computed one -- the ban is on the call itself, not on
# whatever string it would have been passed, so string concatenation/
# obfuscation doesn't reopen this).
BANNED_CALL_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
    }
)

# Any dunder attribute access (`.attr` both starting and ending with `__`)
# is banned outright -- `run_backtest(data, config) -> dict` has no
# legitimate need to ever write `x.__globals__`, `x.__closure__`,
# `x.__class__`, `x.__base__`, `x.__subclasses__`, `x.__code__`,
# `x.__dict__`, `x.__mro__`, etc. in source form (the interpreter still
# invokes dunder *methods* implicitly for operators/`len()`/iteration/etc.
# -- this only bans *explicit* dunder attribute access in the code's own
# source, which is exactly the sandbox-escape surface and not something a
# real strategy implementation writes). This one rule closes every
# reflection-based escape found during adversarial testing, including
# `().__class__.__base__.__subclasses__()`, in a single general check
# rather than an allowlist of specific dangerous names that new Python
# versions or object types could grow more of over time.
_DUNDER_ATTRIBUTE_PATTERN = re.compile(r"^__.+__$")


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
        elif isinstance(node, ast.Attribute):
            if _DUNDER_ATTRIBUTE_PATTERN.match(node.attr):
                errors.append(f"banned attribute access: .{node.attr}")
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
