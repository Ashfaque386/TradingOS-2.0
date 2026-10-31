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

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.base import BrokerAdapter, OrderRequest
from src.models.shadow_mode_run import ShadowModeConfidence, ShadowModeOutcome, ShadowModeRun

_FAILURE_REASON_MAX = 500


async def run_shadow_order_check(
    db: AsyncSession,
    *,
    adapter: BrokerAdapter,
    order: OrderRequest,
    strategy_id: uuid.UUID | None = None,
) -> ShadowModeRun:
    """Dry-run one order. A check that fails is recorded as a failed run with its reason, not
    raised and lost: the Go-Live gate counts consecutive clean days, and a day on which the check
    was tried and broke has to be told apart from a day nobody tried. `strategy_id` ties the run to
    the strategy whose readiness it counts toward."""
    payload: dict[str, Any] = {}
    broker_response: dict[str, Any] | None = None
    outcome = ShadowModeOutcome.OK
    failure_reason: str | None = None
    confidence = (
        ShadowModeConfidence.REAL_SANDBOX_DRY_RUN
        if adapter.has_sandbox
        else ShadowModeConfidence.LOCAL_PAYLOAD_ONLY
    )

    try:
        # An adapter that must look something up before it can build the real payload (Upstox: the
        # instrument key for a symbol) offers `prepare_order_payload`; record what will really be
        # sent.
        prepare = getattr(adapter, "prepare_order_payload", None)
        payload = await prepare(order) if prepare else adapter.build_order_payload(order)
        if adapter.has_sandbox:
            result = await adapter.place_order(order)
            broker_response = result.raw
        # else: no network call happens on this path, by construction -- there is no sandbox
        # endpoint for this broker to call, so pretending otherwise (e.g. by calling place_order
        # against production) would place a REAL order under the banner of a "dry run".
    except Exception as exc:  # noqa: BLE001 - any failure of the dry run is the run's result
        outcome = ShadowModeOutcome.FAILED
        failure_reason = str(exc)[:_FAILURE_REASON_MAX] or exc.__class__.__name__

    run = ShadowModeRun(
        broker_name=adapter.broker_name,
        has_sandbox=adapter.has_sandbox,
        confidence=confidence,
        symbol=order.symbol,
        side=order.side.value,
        quantity=order.quantity,
        order_payload=payload,
        broker_response=broker_response,
        strategy_id=strategy_id,
        outcome=outcome,
        failure_reason=failure_reason,
    )
    db.add(run)

    await write_audit_entry(
        db,
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
            "outcome": outcome,
            "failure_reason": failure_reason,
            "strategy_id": str(strategy_id) if strategy_id else None,
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

    await db.commit()
    await db.refresh(run)
    return run
