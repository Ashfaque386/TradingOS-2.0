"""Backtests on the daily prices the data lake holds: a run made from a symbol and two dates, a
walk-forward and an optimisation that read the same prices, and what each refuses.

The lake is a temporary directory the settings are pointed at. Its bars carry a recorded real
source; bars without one are the generated ones the lake used to be filled with, and are never read.
"""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from httpx import AsyncClient

from src.core.config import get_settings
from src.core.roles import Role
from src.data import lake

DAYS = pd.bdate_range("2024-01-01", periods=120)  # to 2024-06-14
FIRST, LAST = DAYS[0].date().isoformat(), DAYS[-1].date().isoformat()


def _frame(source: str | None = "zerodha", days=DAYS) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    close = 100 * np.cumprod(1 + rng.normal(0.0006, 0.012, len(days)))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 25_000,
        },
        index=pd.DatetimeIndex(days),
    )
    if source is not None:
        frame["source"] = source
    return frame


@pytest.fixture
def the_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lake"
    monkeypatch.setattr(get_settings(), "data_lake_path", str(root))
    monkeypatch.setattr(get_settings(), "paper_trading_synthetic", False)
    lake.write_daily_bars(root, "LAKESTOCK", _frame())
    return root


async def _operator(client: AsyncClient, make_user, role=Role.PORTFOLIO_MANAGER) -> dict[str, str]:
    await make_user("quant@example.com", "supersecret1", role)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "quant@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _version(client: AsyncClient, headers) -> str:
    resp = await client.post(
        "/api/v1/strategies", json={"name": "LakeS", "objective": "obj"}, headers=headers
    )
    assert resp.status_code == 201
    return resp.json()["versions"][0]["id"]


def _body(version: str, **over) -> dict:
    return {
        "strategy_version_id": version,
        "symbol": "LAKESTOCK",
        "start_date": FIRST,
        "end_date": LAST,
        **over,
    }


async def _run(client, headers, version, **over) -> dict:
    resp = await client.post(
        "/api/v1/backtests/from-lake", json=_body(version, **over), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- a run from the lake ----


async def test_a_run_from_the_lake_is_completed_and_keeps_every_trade_with_its_dates(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    run = await _run(client, headers, version)

    assert run["status"] == "completed"
    assert run["data_source"] == "real"
    assert run["start_date"] == FIRST and run["end_date"] == LAST
    assert run["metrics"]["num_trades"] == len(run["trades"]) == len(run["trade_pnls"]) > 0
    for trade, pnl in zip(run["trades"], run["trade_pnls"], strict=True):
        assert trade["net_pnl"] == pnl
        assert FIRST <= trade["entry_date"] <= trade["exit_date"] <= LAST
        assert trade["quantity"] > 0 and trade["costs"] > 0
        assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["costs"])


async def test_a_run_says_how_its_signals_were_made(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    run = await _run(
        client, headers, version, sma_window=10, is_delivery=False, initial_capital=250000
    )

    assert run["signal_config"] == {
        "strategy": "sma_crossover",
        "sma_window": 10,
        "is_delivery": False,
        "initial_capital": 250000,
        "source": "lake",
    }


async def test_only_the_asked_window_is_read(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    run = await _run(client, headers, version, start_date="2024-03-01", end_date="2024-05-31")

    assert run["start_date"] == "2024-03-01" and run["end_date"] == "2024-05-31"


async def test_the_run_is_listed_and_can_be_read_back_with_its_trades(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)
    run = await _run(client, headers, version)

    listed = (await client.get("/api/v1/backtests", headers=headers)).json()
    fetched = (await client.get(f"/api/v1/backtests/{run['id']}", headers=headers)).json()

    assert [r["id"] for r in listed] == [run["id"]]
    assert fetched["trades"] == run["trades"]


async def test_a_run_with_the_caller_posted_bars_also_keeps_its_trades_and_config(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)
    bars = [
        {
            "date": d.date().isoformat(),
            "open": float(r.close),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": 25_000,
        }
        for d, r in _frame().iterrows()
    ]

    resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version,
            "symbol": "POSTED",
            "bars": bars,
            "as_of": LAST,
        },
        headers=headers,
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["data_source"] == "supplied"
    assert body["signal_config"]["source"] == "supplied"
    assert len(body["trades"]) == len(body["trade_pnls"]) > 0


# ---- what it refuses ----


async def test_bars_without_a_recorded_real_source_are_not_read(client, make_user, the_lake):
    lake.write_daily_bars(the_lake, "GENERATED", _frame(source=None))
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/from-lake", json=_body(version, symbol="GENERATED"), headers=headers
    )

    assert resp.status_code == 422
    assert "holds 0 daily bars of GENERATED" in resp.json()["detail"]
    assert "with a recorded real source" in resp.json()["detail"]


async def test_a_symbol_the_lake_does_not_hold_says_so_and_how_many_there_are(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/from-lake", json=_body(version, symbol="NOSUCH"), headers=headers
    )

    assert resp.status_code == 422
    assert "holds 0 daily bars of NOSUCH" in resp.json()["detail"]


async def test_a_window_too_short_to_test_on_is_refused_with_the_count(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/from-lake",
        json=_body(version, start_date="2024-01-01", end_date="2024-01-12"),
        headers=headers,
    )

    assert resp.status_code == 422
    assert "holds 10 daily bars" in resp.json()["detail"]
    assert "at least 30" in resp.json()["detail"]


async def test_an_end_before_the_start_is_refused(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/from-lake",
        json=_body(version, start_date=LAST, end_date=FIRST),
        headers=headers,
    )

    assert resp.status_code == 422


async def test_a_window_the_lake_does_not_reach_up_to_its_end_is_recorded_as_refused(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    # the lake ends on 2024-06-14; asked "as of" the end of July it lacks the day before
    run = await _run(client, headers, version, end_date="2024-07-31")

    assert run["status"] == "refused_stale_data"
    assert "data lake lacks LAKESTOCK data for 2024-07-30" in run["refusal_reason"]
    assert run["metrics"] is None and run["trades"] is None


@pytest.mark.parametrize("role", [Role.RISK_MANAGER, Role.READ_ONLY_AUDITOR])
async def test_only_an_operator_may_run_one(client, make_user, the_lake, role):
    headers = await _operator(client, make_user, role)

    resp = await client.post(
        "/api/v1/backtests/from-lake",
        json=_body("00000000-0000-0000-0000-000000000001"),
        headers=headers,
    )

    assert resp.status_code == 403


# ---- walk-forward and optimisation on the lake's prices ----


async def test_a_walk_forward_without_bars_reads_the_runs_own_window_from_the_lake(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)
    run = await _run(client, headers, version)

    resp = await client.post(
        f"/api/v1/backtests/{run['id']}/walk-forward",
        json={"train_bars": 40, "test_bars": 20},
        headers=headers,
    )

    assert resp.status_code == 200
    windows = resp.json()["walk_forward_result"]["windows"]
    assert len(windows) >= 2
    assert all(FIRST <= w["train_start"][:10] and w["test_end"][:10] <= LAST for w in windows)


async def test_a_walk_forward_may_name_a_different_strategy_than_the_run_used(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)
    run = await _run(client, headers, version, strategy="always_long")

    resp = await client.post(
        f"/api/v1/backtests/{run['id']}/walk-forward",
        json={"train_bars": 40, "test_bars": 20, "strategy": "sma_crossover", "sma_window": 8},
        headers=headers,
    )

    assert resp.status_code == 200


async def test_a_walk_forward_on_a_refused_run_is_a_conflict_and_on_no_run_a_404(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)
    refused = await _run(client, headers, version, end_date="2024-07-31")

    on_refused = await client.post(
        f"/api/v1/backtests/{refused['id']}/walk-forward",
        json={"train_bars": 40, "test_bars": 20},
        headers=headers,
    )
    on_nothing = await client.post(
        "/api/v1/backtests/00000000-0000-0000-0000-000000000009/walk-forward",
        json={"train_bars": 40, "test_bars": 20},
        headers=headers,
    )

    assert on_refused.status_code == 409
    assert on_nothing.status_code == 404


async def test_an_optimisation_reads_the_lake_by_symbol_and_dates(client, make_user, the_lake):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/optimize",
        json={
            "strategy_version_id": version,
            "symbol": "LAKESTOCK",
            "start_date": FIRST,
            "end_date": LAST,
            "n_trials": 8,
            "sma_window_min": 5,
            "sma_window_max": 20,
            "seed": 3,
        },
        headers=headers,
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["n_trials"] == 8
    assert 5 <= body["best_params"]["window"] <= 20


@pytest.mark.parametrize(
    "extra",
    [
        {},  # neither bars nor a symbol with dates
        {"symbol": "LAKESTOCK", "start_date": FIRST},  # no end
        {"symbol": "LAKESTOCK", "start_date": LAST, "end_date": FIRST},  # backwards
        {
            "symbol": "LAKESTOCK",
            "start_date": FIRST,
            "end_date": LAST,
            "sma_window_min": 30,
            "sma_window_max": 10,
        },
    ],
)
async def test_an_optimisation_needs_prices_or_a_lake_window_and_a_sensible_range(
    client, make_user, the_lake, extra
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/optimize",
        json={"strategy_version_id": version, **extra},
        headers=headers,
    )

    assert resp.status_code == 422


async def test_an_optimisation_on_a_symbol_the_lake_lacks_says_how_many_bars_there_are(
    client, make_user, the_lake
):
    headers = await _operator(client, make_user)
    version = await _version(client, headers)

    resp = await client.post(
        "/api/v1/backtests/optimize",
        json={
            "strategy_version_id": version,
            "symbol": "NOSUCH",
            "start_date": date(2024, 1, 1).isoformat(),
            "end_date": LAST,
        },
        headers=headers,
    )

    assert resp.status_code == 422
    assert "holds 0 daily bars of NOSUCH" in resp.json()["detail"]
