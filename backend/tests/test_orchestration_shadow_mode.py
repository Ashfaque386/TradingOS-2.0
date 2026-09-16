"""Shadow Mode differentiation tests (Build Spec §13): the acceptance-
critical behavior is that a Shadow Mode run's stored `confidence` and
`broker_response` honestly reflect whether a real sandbox call happened
(Upstox) or only a local payload was built (Zerodha) -- and that the
Zerodha path genuinely never touches the network, not just that it
*reports* not touching it.
"""

import httpx
from sqlalchemy import select

from src.brokers.base import BrokerCredentials, OrderRequest, OrderSide
from src.brokers.upstox import UpstoxAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.models.audit_log import AuditLog
from src.models.shadow_mode_run import ShadowModeConfidence
from src.orchestration.shadow_mode import run_shadow_order_check

_CREDENTIALS = BrokerCredentials(api_key="test-key", access_token="test-token")
_ORDER = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=10)


async def test_zerodha_shadow_run_is_local_payload_only_and_never_calls_the_network(
    db_session_factory,
):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"order_id": "SHOULD_NEVER_HAPPEN"}})

    adapter = ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(handler))

    async with db_session_factory() as db:
        run = await run_shadow_order_check(db, adapter=adapter, order=_ORDER)

    assert calls == [], "a broker with no sandbox must never receive a network call"
    assert run.has_sandbox is False
    assert run.confidence == ShadowModeConfidence.LOCAL_PAYLOAD_ONLY
    assert run.broker_response is None
    assert run.order_payload["tradingsymbol"] == "INFY"


async def test_upstox_shadow_run_makes_a_genuine_sandbox_call(db_session_factory):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"order_id": "SANDBOX_OID"}})

    adapter = UpstoxAdapter(_CREDENTIALS, sandbox=True, transport=httpx.MockTransport(handler))

    async with db_session_factory() as db:
        run = await run_shadow_order_check(db, adapter=adapter, order=_ORDER)

    assert len(calls) == 1, "a broker with a real sandbox must make exactly one dry-run call"
    assert run.has_sandbox is True
    assert run.confidence == ShadowModeConfidence.REAL_SANDBOX_DRY_RUN
    assert run.broker_response == {"data": {"order_id": "SANDBOX_OID"}}


async def test_the_two_brokers_are_never_reported_as_equivalent(db_session_factory):
    """The single most important assertion in this module: two Shadow
    Mode runs for the two brokers must differ in stored confidence, not
    just in some incidental detail."""

    def zerodha_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("zerodha adapter must never be called")

    def upstox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"order_id": "OID"}})

    zerodha = ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(zerodha_handler))
    upstox = UpstoxAdapter(
        _CREDENTIALS, sandbox=True, transport=httpx.MockTransport(upstox_handler)
    )

    async with db_session_factory() as db:
        zerodha_run = await run_shadow_order_check(db, adapter=zerodha, order=_ORDER)
        upstox_run = await run_shadow_order_check(db, adapter=upstox, order=_ORDER)

    assert zerodha_run.confidence != upstox_run.confidence
    assert zerodha_run.has_sandbox is False
    assert upstox_run.has_sandbox is True


async def test_shadow_run_writes_an_audit_log_entry_with_the_same_confidence(db_session_factory):
    adapter = ZerodhaKiteAdapter(
        _CREDENTIALS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )

    async with db_session_factory() as db:
        run = await run_shadow_order_check(db, adapter=adapter, order=_ORDER)

        result = await db.execute(select(AuditLog).where(AuditLog.action == "shadow_mode.dry_run"))
        entries = result.scalars().all()

    assert len(entries) == 1
    assert entries[0].entity_id == "zerodha"
    assert entries[0].details["confidence"] == run.confidence
    assert entries[0].details["has_sandbox"] is False
