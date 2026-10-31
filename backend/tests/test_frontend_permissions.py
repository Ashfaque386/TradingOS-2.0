"""`lib/permissions.generated.json` is the console's copy of the RBAC policy table.

The console asks `can(role, 'POST /api/v1/...')` from that file, so it must say exactly what the
backend says. After changing any `register_policy` call, run `python scripts/export_permissions.py`
(from `backend/`) and commit the result.
"""

import importlib.util
import json
from pathlib import Path

from src.core.roles import Role

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "export_permissions.py"


def _script():
    spec = importlib.util.spec_from_file_location("export_permissions", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_permissions_match_the_policy_table() -> None:
    script = _script()
    committed = json.loads(script.OUTPUT.read_text(encoding="utf-8"))

    assert committed == script.build_permissions(), (
        "lib/permissions.generated.json is out of date: "
        "run `python scripts/export_permissions.py` from backend/ and commit it"
    )


def test_every_entry_names_real_roles_and_a_served_route() -> None:
    script = _script()
    permissions = script.build_permissions()
    roles = {role.value for role in Role}

    assert permissions, "the policy table is empty"
    for action, allowed in permissions.items():
        method, path = action.split(" ", 1)
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, action
        assert path.startswith("/api/v1/"), action
        assert allowed and set(allowed) <= roles, (action, allowed)
