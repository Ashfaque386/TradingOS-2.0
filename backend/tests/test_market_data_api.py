"""Market Data API acceptance tests (Build Spec §14): Market Pulse and
freshness/instrument/provenance reads, plus every manual ingestion trigger
-- through the actual HTTP routes, RBAC included.
"""

import httpx
import pandas as pd
import pytest
from httpx import AsyncClient

from src.api.routes.market_data import (
    get_data_lake_backup_root,
    get_data_lake_root,
    get_market_data_broker_adapter,
)
from src.brokers.base import BrokerCredentials
from src.brokers.upstox import UpstoxAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.core.roles import Role
from src.data import lake
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


def _linear_ramp_bars(n: int, start: str) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.0 + i for i in range(n)],
            "volume": [1000] * n,
        },
        index=idx,
    )


async def test_indicators_endpoint_returns_real_sma_and_bollinger_middle_once_window_fills(
    client: AsyncClient, make_user, tmp_path
):
    lake.write_daily_bars(tmp_path / "lake", "INDSTOCK", _linear_ramp_bars(40, "2026-07-01"))

    await make_user("md7@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "md7@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/market-data/indicators/INDSTOCK?lookback_days=120&sma_window=20&bollinger_window=20",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["symbol"] == "INDSTOCK"
    assert len(body["dates"]) == 40
    assert len(body["close"]) == 40
    # A 20-day SMA/Bollinger-middle must be null for the first 19 points --
    # no min_periods=1 shortcut fabricating a partial-window average.
    assert all(v is None for v in body["sma"][:19])
    assert body["sma"][19] == pytest.approx(sum(range(20)) / 20 + 100.0)
    assert body["bollinger_middle"][19] == pytest.approx(body["sma"][19])
    assert body["bollinger_upper"][19] > body["bollinger_middle"][19]
    assert body["bollinger_lower"][19] < body["bollinger_middle"][19]
    # EMA/RSI/MACD all present with the same date-aligned length.
    assert len(body["ema"]) == 40
    assert len(body["rsi"]) == 40
    assert len(body["macd"]) == 40
    # A strictly-rising close series' RSI is genuinely 100, not undefined.
    assert body["rsi"][-1] == pytest.approx(100.0)


async def test_indicators_endpoint_returns_empty_series_for_an_uningested_symbol(
    client: AsyncClient, make_user
):
    await make_user("md8@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "md8@example.com", "supersecret1")

    resp = await client.get("/api/v1/market-data/indicators/NEVERINGESTED", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NEVERINGESTED"
    assert body["dates"] == []
    assert body["close"] == []
    assert body["sma"] == []
    assert body["bollinger_upper"] == []


async def test_indicators_endpoint_accessible_to_every_role(client: AsyncClient, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"md9-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/market-data/indicators/ANYSTOCK", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied read access to indicators"


def _clear_broker_adapter_override():
    app.dependency_overrides.pop(get_market_data_broker_adapter, None)


async def test_live_option_chain_returns_503_when_no_broker_configured(
    client: AsyncClient, make_user
):
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: None
    try:
        await make_user("oc1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "oc1@example.com", "supersecret1")
        resp = await client.get(
            "/api/v1/market-data/option-chain/NIFTY?expiry=2026-12-31", headers=_auth(token)
        )
        assert resp.status_code == 503
    finally:
        _clear_broker_adapter_override()


async def test_live_option_chain_returns_real_zerodha_ltp_and_oi_never_iv(
    client: AsyncClient, make_user
):
    """Phase 17 follow-up: Zerodha's option chain is now real (NFO
    instrument dump + batch quote), not a NotImplementedError -- OI is
    real, IV is always null (Kite Connect has no greeks field), same as
    the dedicated adapter-level coverage in test_brokers_zerodha.py. This
    exercises it through the actual API route rather than the adapter in
    isolation."""
    nfo_csv = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
        "tick_size,lot_size,instrument_type,segment,exchange\n"
        "1,1,NIFTY25JAN24000CE,NIFTY,0,2025-01-30,24000.000000,0.05,50,CE,NFO-OPT,NFO\n"
        "2,1,NIFTY25JAN24000PE,NIFTY,0,2025-01-30,24000.000000,0.05,50,PE,NFO-OPT,NFO\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/instruments/NFO":
            return httpx.Response(200, text=nfo_csv)
        if request.url.path == "/quote":
            requested = request.url.params.get_list("i")
            data = {
                "NFO:NIFTY25JAN24000CE": {"last_price": 120.5, "oi": 45000},
                "NFO:NIFTY25JAN24000PE": {"last_price": 95.0, "oi": 38000},
                "NIFTY": {"last_price": 24010.0, "depth": {}},
            }
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {key: value for key, value in data.items() if key in requested},
                },
            )
        raise AssertionError(f"unexpected request path {request.url.path}")

    import src.brokers.zerodha as zerodha_module

    zerodha_module._nfo_instruments_cache = None
    zerodha_module._nfo_instruments_cache_at = 0.0
    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: adapter
    try:
        await make_user("oc2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "oc2@example.com", "supersecret1")
        resp = await client.get(
            "/api/v1/market-data/option-chain/NIFTY?expiry=2025-01-30", headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["broker"] == "zerodha"
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["call_ltp"] == 120.5
        assert entry["call_oi"] == 45000
        assert entry["call_iv"] is None
        assert entry["put_iv"] is None
        assert body["underlying_ltp"] == 24010.0
        assert body["atm_strike"] == 24000.0
    finally:
        _clear_broker_adapter_override()
        zerodha_module._nfo_instruments_cache = None
        zerodha_module._nfo_instruments_cache_at = 0.0


def _upstox_option_chain_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v2/option/chain":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "strike_price": 23500,
                        "call_options": {
                            "instrument_key": "NSE_FO|CALL_LOW",
                            "market_data": {"ltp": 340.0, "oi": 12000.0},
                            "option_greeks": {"iv": 13.1},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|PUT_LOW",
                            "market_data": {"ltp": 40.0, "oi": 22000.0},
                            "option_greeks": {"iv": 13.5},
                        },
                    },
                    {
                        "strike_price": 24000,
                        "call_options": {
                            "instrument_key": "NSE_FO|CALL1",
                            "market_data": {"ltp": 120.5, "oi": 45000.0},
                            "option_greeks": {"iv": 14.2},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|PUT1",
                            "market_data": {"ltp": 95.0, "oi": 38000.0},
                            "option_greeks": {"iv": 15.9},
                        },
                    },
                    {
                        "strike_price": 24500,
                        "call_options": {
                            "instrument_key": "NSE_FO|CALL_HIGH",
                            "market_data": {"ltp": 30.0, "oi": 30000.0},
                            "option_greeks": {"iv": 14.9},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|PUT_HIGH",
                            "market_data": {"ltp": 350.0, "oi": 26000.0},
                            "option_greeks": {"iv": 15.1},
                        },
                    },
                ]
            },
        )
    if request.url.path == "/v2/market-quote/quotes":
        underlying = request.url.params["symbol"]
        return httpx.Response(
            200, json={"data": {underlying: {"last_price": 24080.35, "depth": {}}}}
        )
    raise AssertionError(f"unexpected request path {request.url.path}")


async def test_live_option_chain_returns_real_upstox_oi_iv_ltp(client: AsyncClient, make_user):
    adapter = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(_upstox_option_chain_response),
    )
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: adapter
    try:
        await make_user("oc3@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "oc3@example.com", "supersecret1")
        resp = await client.get(
            "/api/v1/market-data/option-chain/NSE_INDEX|Nifty%2050?expiry=2026-12-31",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["broker"] == "upstox"
        assert len(body["entries"]) == 3
        entry = next(e for e in body["entries"] if e["strike"] == 24000.0)
        assert entry["call_ltp"] == 120.5
        assert entry["call_oi"] == 45000.0
        assert entry["call_iv"] == 14.2
        assert entry["put_oi"] == 38000.0
        assert entry["put_iv"] == 15.9
        # Real spot lookup (Phase 17): 24080.35 is closer to strike 24000
        # than to 23500 or 24500, so that's the honestly-computed ATM strike.
        assert body["underlying_ltp"] == 24080.35
        assert body["atm_strike"] == 24000.0
    finally:
        _clear_broker_adapter_override()


async def test_live_option_chain_leaves_atm_null_when_spot_lookup_fails(
    client: AsyncClient, make_user
):
    """A best-effort spot lookup failing must never fail the whole chain
    response -- the real per-strike data is still worth returning, with
    underlying_ltp/atm_strike honestly null rather than fabricated."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/option/chain":
            return _upstox_option_chain_response(request)
        return httpx.Response(500, text="upstox quote endpoint down")

    adapter = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: adapter
    try:
        await make_user("oc3b@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "oc3b@example.com", "supersecret1")
        resp = await client.get(
            "/api/v1/market-data/option-chain/NSE_INDEX|Nifty%2050?expiry=2026-12-31",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) == 3
        assert body["underlying_ltp"] is None
        assert body["atm_strike"] is None
    finally:
        _clear_broker_adapter_override()


async def test_live_option_expiries_returns_real_data_for_upstox(client: AsyncClient, make_user):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"expiry": "2026-12-31"}, {"expiry": "2026-11-26"}]}
        )

    adapter = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: adapter
    try:
        await make_user("oc4@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "oc4@example.com", "supersecret1")
        resp = await client.get(
            "/api/v1/market-data/option-expiries/NSE_INDEX|Nifty%2050", headers=_auth(token)
        )
        assert resp.status_code == 200
        assert resp.json() == ["2026-11-26", "2026-12-31"]
    finally:
        _clear_broker_adapter_override()


async def test_live_option_chain_readable_by_every_role(client: AsyncClient, make_user):
    app.dependency_overrides[get_market_data_broker_adapter] = lambda: None
    try:
        for i, role in enumerate(
            [
                Role.SYSTEM_ADMINISTRATOR,
                Role.PORTFOLIO_MANAGER,
                Role.RISK_MANAGER,
                Role.READ_ONLY_AUDITOR,
            ]
        ):
            email = f"oc5-{i}@example.com"
            await make_user(email, "supersecret1", role)
            token = await _login(client, email, "supersecret1")
            # 503 (no broker configured) is a real, RBAC-passed response --
            # a 403 would mean the role was denied access to the route
            # itself, which is what this test actually checks.
            resp = await client.get(
                "/api/v1/market-data/option-chain/NIFTY?expiry=2026-12-31", headers=_auth(token)
            )
            assert resp.status_code == 503, f"role {role} was denied read access"
    finally:
        _clear_broker_adapter_override()
