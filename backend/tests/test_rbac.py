import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.rbac import Role, require_role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


@pytest.mark.parametrize(
    "role",
    [Role.PORTFOLIO_MANAGER, Role.RISK_MANAGER, Role.READ_ONLY_AUDITOR],
)
async def test_non_admin_role_gets_403_on_admin_only_route(client, make_user, role):
    await make_user("nonadmin@example.com", "supersecret1", role)
    token = await _login(client, "nonadmin@example.com", "supersecret1")

    resp = await client.get("/api/v1/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_system_administrator_allowed_on_admin_only_route(client, make_user):
    await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin@example.com", "supersecret1")

    resp = await client.get("/api/v1/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "role": "SystemAdministrator"}


async def test_admin_route_requires_authentication(client):
    resp = await client.get("/api/v1/admin/ping")
    assert resp.status_code in (401, 403)


async def test_route_with_no_registered_policy_default_denies(
    make_user, db_session_factory: async_sessionmaker[AsyncSession]
):
    """A route wired to require_role but never registered in the policy
    table must deny everyone, including a SystemAdministrator — the
    fail-closed behaviour the policy-engine design depends on.
    """
    user = await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)

    from src.api.deps import get_current_user

    probe_app = FastAPI()

    @probe_app.get("/unregistered")
    async def _unregistered(current_user=Depends(require_role)):
        return {"ok": True}

    probe_app.dependency_overrides[get_current_user] = lambda: user

    async with AsyncClient(
        transport=ASGITransport(app=probe_app), base_url="http://test"
    ) as probe_client:
        resp = await probe_client.get("/unregistered")

    assert resp.status_code == 403


def test_admin_ping_policy_is_registered_for_system_administrator_only():
    # Import triggers admin.py's module-level register_policy call.
    import src.api.routes.admin  # noqa: F401
    from src.core.rbac import get_policy_table

    policy = get_policy_table()
    assert policy[("GET", "/api/v1/admin/ping")] == frozenset({Role.SYSTEM_ADMINISTRATOR})
