"""Demo SystemAdministrator-only route, exercised by the RBAC tests. Later
phases' real admin endpoints (Agent Gateway config, user management, etc.)
follow this same pattern: register the route's policy once here-style,
enforce it via Depends(require_role).
"""

from fastapi import APIRouter, Depends

from src.core.rbac import Role, register_policy, require_role
from src.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])

register_policy("GET", "/api/v1/admin/ping", roles=[Role.SYSTEM_ADMINISTRATOR])


@router.get("/ping")
async def ping(current_user: User = Depends(require_role)) -> dict:
    return {"status": "ok", "role": current_user.role.value}
