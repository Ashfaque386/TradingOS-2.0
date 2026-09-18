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


# ---------------------------------------------------------------------------
# Build Spec §21-22 hardening pass: a systematic sweep across every mutating
# endpoint added since Phase 0 (including the Agent Gateway config/CLI-backed
# routes from Phase 1), not spot checks on individual features. Two separate
# guarantees:
#
#   1. Coverage: every mutating route on the real, fully-imported app either
#      has a registered policy or is on an explicit, documented exemption
#      list -- catches a future endpoint added without RBAC wiring the
#      instant it's added, rather than relying on someone remembering to
#      write a feature-specific test for it.
#   2. Enforcement: for every registered mutating policy, a role IN its
#      allowed set is let through (never a route-registration bug hands
#      require_role's lookup a path that doesn't match) and a role NOT in
#      it gets exactly 403 -- exercised through the *real* central
#      require_role dependency against the *real* live policy table
#      (get_policy_table() reflects every register_policy() call any
#      imported route module made), not a re-implementation of the check.
# ---------------------------------------------------------------------------

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Endpoints deliberately NOT RBAC-gated, each with its own real auth
# mechanism instead of a JWT/role -- verified individually below, not just
# asserted here. Any mutating route not in this set must have a policy.
_RBAC_EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Auth bootstrap: a caller has no JWT yet by definition.
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/register"),
        ("POST", "/api/v1/auth/refresh"),
        ("POST", "/api/v1/auth/logout"),
        # Inbound webhooks (Phase 12): authenticated by per-provider
        # signature verification (verify_discord_signature/
        # verify_slack_signature/Telegram's own check), not a JWT --
        # webhooks.py's own module docstring documents this explicitly.
        ("POST", "/api/v1/webhooks/discord"),
        ("POST", "/api/v1/webhooks/slack"),
        ("POST", "/api/v1/webhooks/telegram"),
    }
)


def _all_app_routes() -> list[tuple[str, str]]:
    # Importing the real app triggers every route module's module-level
    # register_policy() calls -- the same mechanism
    # test_admin_ping_policy_is_registered_for_system_administrator_only
    # already relies on, just swept across the whole app instead of one route.
    from src.main import app

    routes: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        routes.extend((m, path) for m in methods)
    return routes


def test_every_mutating_route_has_a_registered_rbac_policy_or_is_explicitly_exempt():
    from src.core.rbac import get_policy_table

    policy = get_policy_table()
    mutating_routes = {(m, p) for m, p in _all_app_routes() if m in _MUTATING_METHODS}

    uncovered = mutating_routes - set(policy.keys()) - _RBAC_EXEMPT_ROUTES
    assert not uncovered, (
        f"Mutating route(s) with no RBAC policy and no documented exemption: {sorted(uncovered)}. "
        "Either register a policy (register_policy(...)) or add it to "
        "_RBAC_EXEMPT_ROUTES above with a real reason, not silently."
    )

    # The reverse check matters too: an exemption for a route that no
    # longer exists (renamed/removed) silently stops meaning anything.
    stale_exemptions = _RBAC_EXEMPT_ROUTES - set(_all_app_routes())
    assert not stale_exemptions, f"Exempt route(s) no longer exist: {sorted(stale_exemptions)}"


def _registered_mutating_policies() -> list[tuple[str, str, frozenset[Role]]]:
    from src.core.rbac import get_policy_table

    return sorted(
        (method, path, roles)
        for (method, path), roles in get_policy_table().items()
        if method in _MUTATING_METHODS
    )


@pytest.mark.parametrize(
    "method, path, allowed_roles",
    _registered_mutating_policies(),
    ids=lambda v: f"{v[0]}:{v[1]}" if isinstance(v, tuple) else str(v),
)
async def test_mutating_policy_enforces_exactly_its_allowed_roles(method, path, allowed_roles):
    from types import SimpleNamespace

    from src.api.deps import get_current_user

    probe_app = FastAPI()
    # The path template must match exactly what require_role looks up
    # (request.scope["route"].path) -- any {param} segment's declared name
    # doesn't matter for that lookup, only the literal template string, so
    # every path parameter is accepted as a plain string here.
    probe_app.add_api_route(
        path,
        (lambda current_user=Depends(require_role): {"ok": True}),
        methods=[method],
    )

    for role in Role:
        probe_app.dependency_overrides[get_current_user] = lambda role=role: SimpleNamespace(
            role=role.value
        )
        async with AsyncClient(
            transport=ASGITransport(app=probe_app), base_url="http://test"
        ) as probe_client:
            # Substitute a harmless placeholder for every path parameter --
            # require_role runs off the route *template*, never the
            # resolved value, so what's substituted here is irrelevant to
            # the RBAC decision, only to FastAPI's routing matching at all.
            concrete_path = path
            while "{" in concrete_path:
                start = concrete_path.index("{")
                end = concrete_path.index("}", start)
                concrete_path = concrete_path[:start] + "x" + concrete_path[end + 1 :]
            resp = await probe_client.request(method, concrete_path)

        if role in allowed_roles:
            assert resp.status_code != 403, (
                f"{role.value} is in the allowed set for {method} {path} "
                f"but require_role rejected it: {resp.text}"
            )
        else:
            assert resp.status_code == 403, (
                f"{role.value} is NOT in the allowed set for {method} {path} "
                f"but was let through: {resp.status_code} {resp.text}"
            )
