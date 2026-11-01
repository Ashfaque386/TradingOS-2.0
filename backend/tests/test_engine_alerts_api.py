"""GET /api/v1/engine-alerts and /engine-alerts/alertmanager (src.api.routes.engine_alerts)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.api.routes.engine_alerts import get_alertmanager_http_client
from src.core.config import get_settings
from src.core.roles import Role
from src.main import app
from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.alertmanager_token import ensure_token

T0 = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)


async def _login(client, make_user, role=Role.READ_ONLY_AUDITOR):
    await make_user("who@example.com", "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "who@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed(db_session_factory, *rows):
    async with db_session_factory() as db:
        db.add_all(rows)
        await db.commit()


def _event(key, *, minutes=0, resolved=False, severity="warning", source="watcher", title=None):
    opened = T0 + timedelta(minutes=minutes)
    return EngineAlertEvent(
        key=key,
        source=source,
        kind="halted",
        severity=severity,
        title=title or key,
        detail="why",
        opened_at=opened,
        last_seen_at=opened,
        resolved_at=opened + timedelta(minutes=5) if resolved else None,
    )


# ---- the list ----


async def test_the_list_needs_a_signed_in_user(client):
    assert (await client.get("/api/v1/engine-alerts")).status_code == 401


async def test_every_role_may_read_it(client, make_user, db_session_factory):
    headers = await _login(client, make_user, Role.READ_ONLY_AUDITOR)

    resp = await client.get("/api/v1/engine-alerts", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"open_count": 0, "critical_open_count": 0, "alerts": []}


async def test_open_alerts_come_first_then_the_newest(client, make_user, db_session_factory):
    await _seed(
        db_session_factory,
        _event("old-open", minutes=0),
        _event("new-open", minutes=30),
        _event("resolved-newest", minutes=60, resolved=True),
        _event("resolved-older", minutes=10, resolved=True),
    )
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts", headers=headers)).json()

    assert [a["key"] for a in body["alerts"]] == [
        "new-open",
        "old-open",
        "resolved-newest",
        "resolved-older",
    ]


async def test_the_state_filter_narrows_the_list_but_not_the_count(
    client, make_user, db_session_factory
):
    await _seed(
        db_session_factory,
        _event("open-1", minutes=1, severity="critical"),
        _event("open-2", minutes=2),
        _event("done", minutes=3, resolved=True),
    )
    headers = await _login(client, make_user)

    open_only = (await client.get("/api/v1/engine-alerts?state=open", headers=headers)).json()
    resolved = (await client.get("/api/v1/engine-alerts?state=resolved", headers=headers)).json()

    assert sorted(a["key"] for a in open_only["alerts"]) == ["open-1", "open-2"]
    assert [a["key"] for a in resolved["alerts"]] == ["done"]
    assert (open_only["open_count"], open_only["critical_open_count"]) == (2, 1)
    assert (resolved["open_count"], resolved["critical_open_count"]) == (2, 1)


async def test_the_count_is_the_real_number_whatever_the_limit(
    client, make_user, db_session_factory
):
    await _seed(db_session_factory, *[_event(f"k{n}", minutes=n) for n in range(5)])
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts?limit=2", headers=headers)).json()

    assert len(body["alerts"]) == 2
    assert body["open_count"] == 5


@pytest.mark.parametrize("query", ["state=bogus", "limit=0", "limit=501"])
async def test_a_bad_query_is_refused(client, make_user, query):
    headers = await _login(client, make_user)

    assert (await client.get(f"/api/v1/engine-alerts?{query}", headers=headers)).status_code == 422


async def test_an_alert_is_described_in_full(client, make_user, db_session_factory):
    await _seed(
        db_session_factory, _event("k", severity="critical", source="alertmanager", title="T")
    )
    headers = await _login(client, make_user)

    (alert,) = (await client.get("/api/v1/engine-alerts", headers=headers)).json()["alerts"]

    assert alert["key"] == "k"
    assert alert["source"] == "alertmanager"
    assert alert["kind"] == "halted"
    assert alert["severity"] == "critical"
    assert alert["title"] == "T"
    assert alert["detail"] == "why"
    assert alert["subscription_id"] is None
    assert alert["resolved_at"] is None
    assert alert["notified_at"] is None
    assert alert["opened_at"].startswith("2026-11-02T04:00:00")


# ---- the Alertmanager link ----


def _alertmanager(monkeypatch, tmp_path, *, url="http://alertmanager:9093", transport=None):
    path = tmp_path / "alertmanager_token" / "webhook_token"
    monkeypatch.setenv("ALERTMANAGER_TOKEN_PATH", str(path))
    monkeypatch.setenv("ALERTMANAGER_URL", url)
    get_settings.cache_clear()
    if transport is not None:

        async def client_with_transport():
            async with httpx.AsyncClient(transport=transport) as client:
                yield client

        app.dependency_overrides[get_alertmanager_http_client] = client_with_transport
    return path


@pytest.fixture(autouse=True)
def _reset():
    yield
    app.dependency_overrides.pop(get_alertmanager_http_client, None)
    get_settings.cache_clear()


async def test_a_deployment_without_alertmanager_says_so(client, make_user, monkeypatch, tmp_path):
    _alertmanager(monkeypatch, tmp_path, url="")
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts/alertmanager", headers=headers)).json()

    assert body["configured"] is False
    assert body["reachable"] is None
    assert "ALERTMANAGER_URL" in body["detail"]


async def test_alertmanager_up_with_a_token_and_a_recent_alert(
    client, make_user, db_session_factory, monkeypatch, tmp_path
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="OK")

    path = _alertmanager(monkeypatch, tmp_path, transport=httpx.MockTransport(handler))
    ensure_token(path)
    await _seed(
        db_session_factory,
        _event("alertmanager:a", minutes=0, source="alertmanager", resolved=True),
        _event("alertmanager:b", minutes=20, source="alertmanager"),
        _event("watcher-noise", minutes=90),
    )
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts/alertmanager", headers=headers)).json()

    assert seen == ["http://alertmanager:9093/-/healthy"]
    assert body["configured"] is True
    assert body["reachable"] is True
    assert body["detail"] == "Alertmanager is up."
    assert body["token_ready"] is True
    # The newest one that came THROUGH Alertmanager, not the watcher's.
    assert body["last_alert_received_at"].startswith("2026-11-02T04:20:00")


async def test_alertmanager_that_cannot_be_reached_is_reported(
    client, make_user, monkeypatch, tmp_path
):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _alertmanager(monkeypatch, tmp_path, transport=httpx.MockTransport(refuse))
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts/alertmanager", headers=headers)).json()

    assert body["configured"] is True
    assert body["reachable"] is False
    assert body["detail"] == "Could not reach Alertmanager: ConnectError"
    assert body["token_ready"] is False
    assert body["last_alert_received_at"] is None


async def test_alertmanager_answering_with_an_error_is_not_up(
    client, make_user, monkeypatch, tmp_path
):
    _alertmanager(
        monkeypatch, tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(503))
    )
    headers = await _login(client, make_user)

    body = (await client.get("/api/v1/engine-alerts/alertmanager", headers=headers)).json()

    assert body["reachable"] is False
    assert body["detail"] == "Alertmanager answered 503."


async def test_a_trailing_slash_in_the_address_does_not_double_up(
    client, make_user, monkeypatch, tmp_path
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    _alertmanager(
        monkeypatch, tmp_path, url="http://am:9093/", transport=httpx.MockTransport(handler)
    )
    headers = await _login(client, make_user)

    await client.get("/api/v1/engine-alerts/alertmanager", headers=headers)

    assert seen == ["http://am:9093/-/healthy"]
