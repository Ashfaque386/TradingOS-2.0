"""Live Canvas (Phase 22, docs/phase20-old-vs-new-comparison.md item 19):
one composed read over real, already-persisted data -- the newest
strategy code, the newest completed backtest, and the newest audit
event, across every strategy/run. Drives a real strategy through the
real sandbox pipeline and a real backtest run, the same way
test_backtests_api.py does, rather than inserting rows directly --
`GET /canvas/state` reads what those real endpoints actually wrote.
"""

import uuid
from datetime import date

import numpy as np
import pandas as pd
from httpx import AsyncClient

from src.core.roles import Role
from src.models.backtest_run import BacktestRun, BacktestStatus


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _bars(n: int = 80, seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    returns = rng.normal(0.0006, 0.012, n)
    close = 100 * np.cumprod(1 + returns)
    return [
        {
            "date": d.date().isoformat(),
            "open": float(c),
            "high": float(c * 1.01),
            "low": float(c * 0.99),
            "close": float(c),
            "volume": 25_000,
        }
        for d, c in zip(idx, close, strict=True)
    ]


async def test_canvas_state_is_empty_shaped_with_no_data(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin1@example.com", "supersecret1")

    resp = await client.get("/api/v1/canvas/state", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_strategy_code"] is None
    assert body["latest_backtest_result"] is None
    # The login itself already wrote an audit row -- never null once any
    # mutation anywhere has ever happened.
    assert body["latest_agent_log"] is not None


async def test_canvas_state_surfaces_the_real_newest_strategy_code_and_backtest(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin2@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Canvas Test Strategy", "objective": "obj"},
        headers=headers,
    )
    assert strategy_resp.status_code == 201
    strategy_body = strategy_resp.json()
    strategy_id = strategy_body["id"]
    version = strategy_body["versions"][0]

    bars = _bars()
    backtest_resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version["id"],
            "symbol": "CANVASSTOCK",
            "bars": bars,
            "strategy": "sma_crossover",
            "as_of": bars[-1]["date"],
        },
        headers=headers,
    )
    assert backtest_resp.status_code == 201
    backtest_body = backtest_resp.json()
    assert backtest_body["status"] == "completed"

    resp = await client.get("/api/v1/canvas/state", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["latest_strategy_code"]["strategy_id"] == strategy_id
    assert body["latest_strategy_code"]["strategy_name"] == "Canvas Test Strategy"
    assert body["latest_strategy_code"]["version_id"] == version["id"]
    assert body["latest_strategy_code"]["code"] == version["code"]

    assert body["latest_backtest_result"]["backtest_id"] == backtest_body["id"]
    assert body["latest_backtest_result"]["strategy_id"] == strategy_id
    assert body["latest_backtest_result"]["symbol"] == "CANVASSTOCK"
    assert body["latest_backtest_result"]["status"] == "completed"
    assert "sharpe" in body["latest_backtest_result"]["metrics"]

    assert body["latest_agent_log"] is not None


async def test_canvas_state_only_surfaces_completed_backtests(
    client, make_user, db_session_factory
):
    """A pending/refused/failed run must never be shown as "the newest
    result" -- an operator glancing at the canvas would read that as a
    real, usable number. Inserted directly (not driven through a real
    backtest attempt): this test is about the canvas's own status filter,
    not about which real inputs make the backtest engine refuse a run --
    that's `test_engine_backtest.py`'s job."""
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin3@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies", json={"name": "S2", "objective": "obj"}, headers=headers
    )
    version_id = strategy_resp.json()["versions"][0]["id"]

    async with db_session_factory() as db:
        db.add(
            BacktestRun(
                id=uuid.uuid4(),
                strategy_version_id=uuid.UUID(version_id),
                symbol="REFUSEDSTOCK",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 31),
                status=BacktestStatus.REFUSED_STALE_DATA,
                refusal_reason="test fixture: not a real refusal",
            )
        )
        await db.commit()

    resp = await client.get("/api/v1/canvas/state", headers=headers)
    body = resp.json()
    assert body["latest_backtest_result"] is None


async def test_canvas_state_requires_authentication(client):
    resp = await client.get("/api/v1/canvas/state")
    assert resp.status_code in (401, 403)
