"""Real-time WebSocket channels (Build Spec §18/Phase 13 frontend wiring):
`activity-feed`, `agent-logs`, `organization-events`, `sign-off-queue`,
`ticks` -- the exact channel names the frontend's own build handoff notes
(`docs/TradingOS_v0_Frontend_Prompt_Pack.md` §"After v0") specify.

Each channel is backed by whatever real mechanism already exists for that
data, not a new parallel event-logging system invented for this phase:

- `activity-feed` / `agent-logs`: both poll the same real, single source
  of truth -- the append-only, hash-chained `audit_log` table (Build Spec
  §19) that every mutating action in this codebase already writes to via
  `src.audit.service.write_audit_entry`. They are two names for the same
  underlying stream (the frontend's own mock-data mapping table groups
  them under one row: "Activity feed, agent status"), not two
  independently-modeled systems -- a DB poll, not Redis pub/sub, so a
  browser that reconnects a few seconds late never silently misses a row
  the way a pub/sub subscriber would.
- `organization-events`: real Redis pub/sub, already published by
  `src.orchestration.events.emit()` on every orchestration node/task
  event (`organization-events:{run_id}`) -- genuinely wired since Phase 2,
  simply never had a WebSocket consumer until now. `psubscribe`s across
  every run when no `run_id` query param is given (the Overview page's
  organization-wide feed), or one run's own channel when scoped (the
  Mission Control task drawer's per-run activity log).
- `sign-off-queue`: polls the same tables `GET /approvals` and
  `GET /live-trading/intents` already query, pushing a full snapshot on
  each tick -- every `LiveOrderIntent.expires_at` in that snapshot is the
  real database timestamp `generate_live_order_intent` wrote, so a page
  refresh reconnects and receives that exact same value, never a
  client-restarted timer.
- `ticks`: `redis.xread(..., block=...)` against the real per-symbol
  Redis Stream Phase 7 already writes to (`src.engine.paper_trading.
  tick_feed`), blocking-polled rather than sleep-looped so a new tick is
  forwarded within one round trip of being written, not up to a whole
  poll interval late.
"""

import asyncio
import json
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.routes.live_trading import _intent_response
from src.api.schemas import ApprovalRequestResponse
from src.api.ws_auth import authenticate_websocket
from src.core.db import get_db, get_session_factory
from src.core.redis_client import get_redis
from src.engine.paper_trading.tick_feed import tick_stream_key
from src.models.approval_request import ApprovalRequest, ApprovalStatus
from src.models.audit_log import AuditLog
from src.models.live_order_intent import LiveOrderIntent

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ws", tags=["websockets"])

_AUDIT_POLL_INTERVAL_SECONDS = 1.5
_AUDIT_INITIAL_BACKFILL_ROWS = 30
_SIGNOFF_POLL_INTERVAL_SECONDS = 2.0


def _audit_row_to_dict(row: AuditLog) -> dict:
    return {
        "id": str(row.id),
        "sequence": row.sequence,
        "actor": row.actor,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details,
        "created_at": row.created_at.isoformat(),
    }


async def _stream_audit_log(websocket: WebSocket, session_factory: async_sessionmaker) -> None:
    async with session_factory() as db:
        backfill = (
            (
                await db.execute(
                    select(AuditLog)
                    .order_by(AuditLog.sequence.desc())
                    .limit(_AUDIT_INITIAL_BACKFILL_ROWS)
                )
            )
            .scalars()
            .all()
        )
    last_sequence = backfill[0].sequence if backfill else 0
    await websocket.send_json(
        {"type": "backfill", "events": [_audit_row_to_dict(row) for row in reversed(backfill)]}
    )

    while True:
        await asyncio.sleep(_AUDIT_POLL_INTERVAL_SECONDS)
        async with session_factory() as db:
            new_rows = (
                (
                    await db.execute(
                        select(AuditLog)
                        .where(AuditLog.sequence > last_sequence)
                        .order_by(AuditLog.sequence)
                    )
                )
                .scalars()
                .all()
            )
        for row in new_rows:
            last_sequence = row.sequence
            await websocket.send_json({"type": "event", "event": _audit_row_to_dict(row)})


@router.websocket("/activity-feed")
async def activity_feed_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> None:
    user = await authenticate_websocket(websocket, db)
    if user is None:
        return
    await websocket.accept()
    try:
        await _stream_audit_log(websocket, session_factory)
    except WebSocketDisconnect:
        pass


@router.websocket("/agent-logs")
async def agent_logs_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> None:
    """Same real source as `/activity-feed` -- see module docstring."""
    user = await authenticate_websocket(websocket, db)
    if user is None:
        return
    await websocket.accept()
    try:
        await _stream_audit_log(websocket, session_factory)
    except WebSocketDisconnect:
        pass


@router.websocket("/organization-events")
async def organization_events_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    user = await authenticate_websocket(websocket, db)
    if user is None:
        return
    await websocket.accept()

    run_id_param = websocket.query_params.get("run_id")
    pattern = f"organization-events:{run_id_param}" if run_id_param else "organization-events:*"

    pubsub = redis.pubsub()
    try:
        await pubsub.psubscribe(pattern)
        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            await websocket.send_json({"type": "event", "event": payload})
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.punsubscribe(pattern)
        await pubsub.aclose()


async def _signoff_snapshot(db: AsyncSession) -> dict:
    approvals = (
        (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
                .order_by(ApprovalRequest.created_at)
            )
        )
        .scalars()
        .all()
    )
    intents = (
        (
            await db.execute(
                select(LiveOrderIntent)
                .where(LiveOrderIntent.status == "pending_approval")
                .order_by(LiveOrderIntent.expires_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "type": "snapshot",
        "server_time": datetime.now(UTC).isoformat(),
        "approvals": [
            json.loads(ApprovalRequestResponse.model_validate(a).model_dump_json())
            for a in approvals
        ],
        "intents": [json.loads(_intent_response(i).model_dump_json()) for i in intents],
    }


@router.websocket("/sign-off-queue")
async def sign_off_queue_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> None:
    user = await authenticate_websocket(websocket, db)
    if user is None:
        return
    await websocket.accept()
    try:
        while True:
            async with session_factory() as session:
                snapshot = await _signoff_snapshot(session)
            await websocket.send_json(snapshot)
            await asyncio.sleep(_SIGNOFF_POLL_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        pass


@router.websocket("/ticks")
async def ticks_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    user = await authenticate_websocket(websocket, db)
    if user is None:
        return

    symbol = websocket.query_params.get("symbol")
    if not symbol:
        await websocket.close(code=1008, reason="missing symbol query param")
        return

    await websocket.accept()
    stream_key = tick_stream_key(symbol)
    last_id = "$"  # only ticks published after this connection opens

    try:
        while True:
            response = await redis.xread({stream_key: last_id}, block=2000, count=100)
            if not response:
                continue
            for _stream, entries in response:
                for entry_id, fields in entries:
                    last_id = entry_id
                    await websocket.send_json(
                        {
                            "symbol": symbol,
                            "price": float(fields["price"]),
                            "timestamp_ms": int(fields["timestamp_ms"]),
                        }
                    )
    except WebSocketDisconnect:
        pass
