"""The matched route's template for a request, as the audit trail and request metrics name it."""

from starlette.types import Scope


def route_template(scope: Scope) -> str | None:
    """The template of the route this request matched (`/api/v1/strategies/{strategy_id}`), or
    None when it matched no route. Only meaningful once the app has handled the request: the
    router puts the route on the scope while matching.

    Same real fix as src.core.rbac.require_role: Phase 26's dependency-CVE pass
    (docs/phase20-old-vs-new-comparison.md item 28) bumped FastAPI/Starlette, and `route.path` on
    a nested APIRoute can come back missing an ancestor `include_router` prefix (confirmed live
    for /api/v1/broker-credentials/*); it is rebuilt from the real request path, which is not
    affected."""
    route = scope.get("route")
    if route is None:
        return None
    template: str = route.path
    real_path = scope.get("path", "")
    if not template.startswith("/api/v1") and real_path.startswith("/api/v1"):
        template = "/api/v1" + template
    return template
