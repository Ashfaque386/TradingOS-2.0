"""code-format-lint (Build Spec §16 starter skill set). A real (not stubbed)
minimal check: the submitted code must parse as valid Python and must not
import any module from the Build Spec §9 static-validation ban-list.
Full AST-ban-list + ruff enforcement for generated strategy code is built
out in the Strategy Pipeline & Sandbox phase (§9) -- this skill is the
general-purpose lint tool any agent can call standalone.
"""

import ast

BANNED_IMPORTS = frozenset({"os", "subprocess", "socket", "sys", "shutil", "ctypes"})


def code_format_lint(params: dict) -> dict:
    code = params.get("code", "")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"valid": False, "errors": [f"SyntaxError: {exc}"], "banned_imports_found": []}

    banned_found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            banned_found.extend(a.name for a in node.names if a.name in BANNED_IMPORTS)
        elif isinstance(node, ast.ImportFrom) and node.module in BANNED_IMPORTS:
            banned_found.append(node.module)

    return {
        "valid": not banned_found,
        "errors": [] if not banned_found else [f"banned import: {name}" for name in banned_found],
        "banned_imports_found": banned_found,
    }
