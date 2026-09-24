"""Policy-engine RBAC: a single, centrally-registered route+method+role table.

Routes declare their required roles once, in POLICY_TABLE below (or by calling
register_policy from another module during import). The `require_role`
dependency is the only enforcement path — there is no per-route ad hoc
`if user.role != ...` check anywhere else in the codebase. Exact
(method, path template) matching, matching Build Spec §20.
"""

from collections.abc import Iterable

from fastapi import Depends, HTTPException, Request, status

from src.api.deps import get_current_user
from src.core.roles import Role
from src.models.user import User

# (HTTP method, route path template) -> roles allowed to call it.
# Path templates are FastAPI's route.path (e.g. "/api/v1/admin/ping"), not the
# raw incoming URL, so path parameters match regardless of their value.
_POLICY_TABLE: dict[tuple[str, str], frozenset[Role]] = {}


def register_policy(method: str, path: str, roles: Iterable[Role]) -> None:
    _POLICY_TABLE[(method.upper(), path)] = frozenset(roles)


def get_policy_table() -> dict[tuple[str, str], frozenset[Role]]:
    return dict(_POLICY_TABLE)


async def require_role(request: Request, current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency enforcing the centrally-registered policy for this route.

    A route wired to this dependency without a registered policy entry is a
    programming error, not an open door: it default-denies.
    """
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    allowed_roles = _POLICY_TABLE.get((request.method.upper(), path))

    if not allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No RBAC policy registered for this route; access denied by default.",
        )

    if Role(current_user.role) not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role is not permitted to perform this action.",
        )

    return current_user
