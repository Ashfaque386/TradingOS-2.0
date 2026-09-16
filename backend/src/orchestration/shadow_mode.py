"""Shadow Mode dry-run of a broker order (Build Spec §13).

Documents, per broker, whether a dry run was a genuine sandbox call
(Upstox has a real sandbox environment) or local payload construction
only (Zerodha has no sandbox at all). These two are never presented as
equivalent anywhere a human or another system reads this: not in the
stored `ShadowModeRun.confidence` field, not in the `AuditLog` entry this
writes alongside it, and not in any API response built from either. A
reader who only sees "Shadow Mode check passed for both brokers" without
also seeing which one actually talked to a broker and which one only
built a payload has been misled -- that is exactly the failure mode this
module exists to prevent.

The caller decides which adapter instance to pass in. For Upstox that
must be an instance pointed at the sandbox base URL
(`build_broker_adapter("upstox", credentials, sandbox=True)`) -- this
function does not second-guess or re-point the adapter it's given, it
only branches on `adapter.has_sandbox` to decide whether calling
`place_order` on that adapter is safe to do at all. Passing a
production-pointed Upstox adapter here would make a REAL live order,
which is precisely why building that adapter correctly is the caller's
responsibility, not something this function silently fixes up.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.brokers.base import BrokerAdapter, OrderRequest
from src.models.audit_log import AuditLog
from src.models.shadow_mode_run import ShadowModeConfidence, ShadowModeRun


async def run_shadow_order_check(
    db: AsyncSession, *, adapter: BrokerAdapter, order: OrderRequest
) -> ShadowModeRun:
    payload = adapter.build_order_payload(order)
    broker_response: dict | None = None

    if adapter.has_sandbox:
        result = await adapter.place_order(order)
        confidence = ShadowModeConfidence.REAL_SANDBOX_DRY_RUN
        broker_response = result.raw
    else:
        # No network call happens on this path, by construction -- there
        # is no sandbox endpoint for this broker to call, so pretending
        # otherwise (e.g. by calling place_order against production) would
        # place a REAL order under the banner of a "dry run".
        confidence = ShadowModeConfidence.LOCAL_PAYLOAD_ONLY

    run = ShadowModeRun(
        broker_name=adapter.broker_name,
        has_sandbox=adapter.has_sandbox,
        confidence=confidence,
        symbol=order.symbol,
        side=order.side.value,
        quantity=order.quantity,
        order_payload=payload,
        broker_response=broker_response,
    )
    db.add(run)

    db.add(
        AuditLog(
            actor="system",
            action="shadow_mode.dry_run",
            entity_type="broker_adapter",
            entity_id=adapter.broker_name,
            details={
                "confidence": confidence,
                "has_sandbox": adapter.has_sandbox,
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "note": (
                    "genuine sandbox dry run against the broker's real sandbox environment"
                    if adapter.has_sandbox
                    else (
                        "local payload construction only -- this broker has "
                        "no sandbox; nothing was sent over the network"
                    )
                ),
            },
        )
    )

    await db.commit()
    await db.refresh(run)
    return run
