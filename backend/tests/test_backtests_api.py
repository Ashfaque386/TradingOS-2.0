"""Backtest & optimization API tests (Build Spec §10): a strategy can be
backtested with realistic Indian costs, walk-forward and Monte Carlo
validated, and compared against another run -- through the actual HTTP
routes, RBAC included.
"""

import numpy as np
import pandas as pd
from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _create_strategy_version(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": "S", "objective": "obj"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    return resp.json()["versions"][0]["id"]


def _bars(n: int = 80, seed: int = 5) -> list[dict]:
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


async def test_full_pipeline_end_to_end_with_realistic_costs(client, make_user):
    await make_user("ops@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)
    bars = _bars()
    as_of = bars[-1]["date"]

    run_resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version_id,
            "symbol": "TESTSTOCK",
            "bars": bars,
            "strategy": "sma_crossover",
            "as_of": as_of,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert run_resp.status_code == 201
    run_body = run_resp.json()
    assert run_body["status"] == "completed"
    assert "sharpe" in run_body["metrics"]
    run_id = run_body["id"]

    wf_resp = await client.post(
        f"/api/v1/backtests/{run_id}/walk-forward",
        json={"bars": bars, "train_bars": 30, "test_bars": 15, "strategy": "sma_crossover"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert wf_resp.status_code == 200
    assert "passed" in wf_resp.json()["walk_forward_result"]

    mc_resp = await client.post(
        f"/api/v1/backtests/{run_id}/monte-carlo",
        json={"n_paths": 500, "seed": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert mc_resp.status_code == 200
    assert "percentile_95_max_drawdown" in mc_resp.json()["monte_carlo_result"]

    get_resp = await client.get(
        f"/api/v1/backtests/{run_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "completed"

    run2_resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version_id,
            "symbol": "TESTSTOCK",
            "bars": bars,
            "strategy": "always_long",
            "as_of": as_of,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert run2_resp.status_code == 201
    run2_id = run2_resp.json()["id"]

    compare_resp = await client.post(
        "/api/v1/backtests/compare",
        json={"run_ids": [run_id, run2_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert compare_resp.status_code == 200
    correlations = compare_resp.json()["correlations"]
    assert len(correlations) == 1
    (correlation,) = correlations.values()
    assert correlation is None or -1.0 <= correlation <= 1.0


async def test_stale_data_is_refused_via_api(client, make_user):
    await make_user("ops2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops2@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)
    bars = _bars()

    resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version_id,
            "symbol": "TESTSTOCK",
            "bars": bars,
            "as_of": "2030-01-02",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "refused_stale_data"
    assert body["metrics"] is None
    assert body["refusal_reason"] is not None


async def test_run_backtest_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")
    bars = _bars()

    resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": "00000000-0000-0000-0000-000000000000",
            "symbol": "TESTSTOCK",
            "bars": bars,
            "as_of": bars[-1]["date"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_get_backtest_run_not_found(client, make_user):
    await make_user("ops3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops3@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/backtests/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_optimize_endpoint_persists_real_sweep(client, make_user):
    await make_user("ops4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops4@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)
    bars = _bars()

    resp = await client.post(
        "/api/v1/backtests/optimize",
        json={
            "strategy_version_id": version_id,
            "bars": bars,
            "n_trials": 6,
            "sma_window_min": 5,
            "sma_window_max": 30,
            "seed": 0,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["n_trials"] == 6
    assert body["param_importance"] is None or "window" in body["param_importance"]
