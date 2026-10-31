"""Write the console's copy of the RBAC policy table: `lib/permissions.generated.json`.

The backend's `register_policy` table is the only place that says which role may call which route.
The console used to repeat those answers by hand (`role === 'SystemAdministrator' || ...`), and the
copies drifted: a button shown to a role the server then refused. This exports the table so the
console asks `can(role, 'POST /api/v1/...')` instead, and a test fails when the file is stale.

    python scripts/export_permissions.py          # rewrite lib/permissions.generated.json
    python scripts/export_permissions.py --check  # exit 1 if the file is out of date
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
OUTPUT = REPO / "lib" / "permissions.generated.json"

# Run as `python scripts/export_permissions.py`, only scripts/ is on the path: add the backend root.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def build_permissions() -> dict[str, list[str]]:
    """{"METHOD /api/v1/path": [role, ...]} for every route with a registered policy, sorted."""
    import src.main  # noqa: F401 - importing the app registers every route module's policies
    from src.core.rbac import get_policy_table

    table = get_policy_table()
    return {
        f"{method} {path}": sorted(role.value for role in roles)
        for (method, path), roles in sorted(table.items())
    }


def render(permissions: dict[str, list[str]]) -> str:
    return json.dumps(permissions, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    rendered = render(build_permissions())
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current.replace("\r\n", "\n") != rendered:
            print(f"{OUTPUT} is out of date: run `python scripts/export_permissions.py`")
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} ({len(json.loads(rendered))} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
