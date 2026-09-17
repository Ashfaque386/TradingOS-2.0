"""Market Data API acceptance tests (Build Spec §14): Market Pulse and
freshness/instrument/provenance reads, plus every manual ingestion trigger
-- through the actual HTTP routes, RBAC included.
"""

import pytest
from httpx import AsyncClient

from src.api.routes.market_data import get_data_lake_backup_root, get_data_lake_root
from src.core.roles import Role
from src.main import app


@pytest.fixture(autouse=True)
def _isolated_data_lake_root(tmp_path):
    """Every route in this file reads/writes the real data lake root via
    these two dependencies -- override them to a per-test tmp_path so
    tests never touch (or pollute) the repo's own data_lake/ directory."""
    app.dependency_overrides[get_data_lake_root] = lambda: tmp_path / "lake"
    app.dependency_overrides[get_data_lake_backup_root] = lambda: tmp_path / "backups"
    yield
    app.dependency_overrides.pop(get_data_lake_root, None)
    app.dependency_overrides.pop(get_data_lake_backup_root, None)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_market_pulse_endpoint_returns_real_shape(client: AsyncClient, make_user):
    await make_user("md1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "md1@example.com", "supersecret1")

    resp = await client.get("/api/v1/market-data/pulse", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert "india_vix" in body
    assert set(body["sector_indices_change_pct"]) == {
        "NIFTY_BANK",
        "NIFTY_IT",
        "NIFTY_AUTO",
        "NIFTY_PHARMA",
        "NIFTY_FMCG",
    }
    assert set(body["global_indices_change_pct"]) == {
        "SP500",
        "NASDAQ",
        "DOWJONES",
        "NIKKEI225",
        "HANGSENG",
    }


async def test_manual_daily_ingestion_trigger_requires_operator_role(
    client: AsyncClient, make_user
):
    await make_user("md2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "md2@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/market-data/ingest/daily",
        json={"symbols": ["DEMOSTOCK"], "as_of": "2026-09-16"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_manual_daily_ingestion_trigger_then_freshness_and_instruments_reflect_it(
    client: AsyncClient, make_user
):
    await make_user("md3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "md3@example.com", "supersecret1")

    ingest_resp = await client.post(
        "/api/v1/market-data/ingest/daily",
        json={"symbols": ["DEMOSTOCK"], "as_of": "2026-09-16"},
        headers=_auth(token),
    )
    assert ingest_resp.status_code == 200
    assert ingest_resp.json()["status"] == "success"

    freshness_resp = await client.get(
        "/api/v1/market-data/freshness/DEMOSTOCK", headers=_auth(token)
    )
    assert freshness_resp.status_code == 200
    records = freshness_resp.json()
    assert len(records) == 1
    assert records[0]["data_date"] == "2026-09-16"

    instrument_sync_resp = await client.post(
        "/api/v1/market-data/ingest/instrument-master",
        json={"symbols": ["DEMOSTOCK"]},
        headers=_auth(token),
    )
    assert instrument_sync_resp.status_code == 200

    instruments_resp = await client.get("/api/v1/market-data/instruments", headers=_auth(token))
    assert instruments_resp.status_code == 200
    symbols = {row["symbol"] for row in instruments_resp.json()}
    assert "DEMOSTOCK" in symbols

    provenance_resp = await client.get("/api/v1/market-data/provenance", headers=_auth(token))
    assert provenance_resp.status_code == 200
    pipelines = {row["pipeline"] for row in provenance_resp.json()}
    assert "incremental_daily" in pipelines
    assert "instrument_master" in pipelines


async def test_daily_ingestion_trigger_skips_holiday(client: AsyncClient, make_user):
    await make_user("md4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "md4@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/market-data/ingest/daily",
        json={"symbols": ["DEMOSTOCK"], "as_of": "2026-01-26"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols_processed"] == 0
    assert body["details"]["skipped_reason"] == "not an NSE trading day"


async def test_bhavcopy_fallback_trigger_honestly_reports_synthetic_source(
    client: AsyncClient, make_user
):
    await make_user("md5@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "md5@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/market-data/bhavcopy-fallback",
        json={"symbols": ["DEMOSTOCK"], "day": "2026-09-16", "segment": "equity"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    # This sandbox has no real egress to NSE's archive, so the real fetch
    # always fails and this must honestly report the synthetic fallback,
    # never silently claim real data.
    assert body["source"] == "bhavcopy_fallback_synthetic"
    assert body["details"]["real_fetch_error"] is not None


async def test_catalog_refresh_and_backup_triggers(client: AsyncClient, make_user):
    await make_user("md6@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "md6@example.com", "supersecret1")

    await client.post(
        "/api/v1/market-data/ingest/daily",
        json={"symbols": ["DEMOSTOCK"], "as_of": "2026-09-16"},
        headers=_auth(token),
    )

    catalog_resp = await client.post("/api/v1/market-data/catalog-refresh", headers=_auth(token))
    assert catalog_resp.status_code == 200
    assert catalog_resp.json()["status"] == "success"

    backup_resp = await client.post("/api/v1/market-data/backup", headers=_auth(token))
    assert backup_resp.status_code == 200
    assert backup_resp.json()["status"] == "success"
