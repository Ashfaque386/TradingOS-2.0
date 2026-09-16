"""Shadow Mode dry-run API (Build Spec §13): runs
src.orchestration.shadow_mode.run_shadow_order_check against whichever
broker the caller names, using that broker's configured credentials from
the secrets store. The adapter this builds is always sandbox-pointed
(`sandbox=True`) regardless of the caller's intent -- a dry run must
never be able to reach production, and that guarantee lives here, at the
one place an adapter gets constructed for this purpose, not left to the
caller to remember.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.api.schemas import ShadowModeCheckRequest, ShadowModeRunResponse
from src.brokers.base import OrderRequest, OrderSide, OrderType
from src.brokers.factory import KNOWN_BROKERS, build_broker_adapter
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.shadow_mode_run import ShadowModeRun
from src.models.user import User
from src.orchestration.shadow_mode import run_shadow_order_check
from src.security.secrets_store import SecretsStore

router = APIRouter(prefix="/shadow-mode", tags=["shadow-mode"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER, Role.RISK_MANAGER]

register_policy("POST", "/api/v1/shadow-mode/check", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/shadow-mode/runs", roles=list(Role))


def _run_response(run: ShadowModeRun) -> ShadowModeRunResponse:
    return ShadowModeRunResponse(
        id=run.id,
        broker_name=run.broker_name,
        has_sandbox=run.has_sandbox,
        confidence=run.confidence,
        symbol=run.symbol,
        side=run.side,
        quantity=run.quantity,
        order_payload=run.order_payload,
        broker_response=run.broker_response,
        created_at=run.created_at.isoformat(),
    )


@router.post("/check", status_code=status.HTTP_201_CREATED)
async def run_shadow_mode_check_endpoint(
    body: ShadowModeCheckRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> ShadowModeRunResponse:
    if body.broker not in KNOWN_BROKERS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown broker: {body.broker!r} (known: {', '.join(KNOWN_BROKERS)})",
        )

    credentials = store.get_credentials(body.broker)
    if credentials is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no credentials configured for broker {body.broker!r} -- "
            "POST /api/v1/broker-credentials/{broker} first",
        )

    adapter = build_broker_adapter(body.broker, credentials, sandbox=True)
    order = OrderRequest(
        symbol=body.symbol,
        side=OrderSide(body.side),
        quantity=body.quantity,
        order_type=OrderType(body.order_type),
        price=body.price,
        product=body.product,
    )
    run = await run_shadow_order_check(db, adapter=adapter, order=order)
    return _run_response(run)


@router.get("/runs")
async def list_shadow_mode_runs_endpoint(
    broker: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[ShadowModeRunResponse]:
    query = select(ShadowModeRun).order_by(ShadowModeRun.created_at.desc())
    if broker is not None:
        query = query.where(ShadowModeRun.broker_name == broker)
    result = await db.execute(query)
    return [_run_response(run) for run in result.scalars().all()]
