"""POST /api/v1/webhooks/alertmanager: what Prometheus's alerts become once Alertmanager forwards
them (src.api.routes.alertmanager_webhook).
"""

import json

import pytest
from sqlalchemy import select

from src.core.config import get_settings
from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.alertmanager_token import ensure_token
from src.orchestration import engine_alerts

URL = "/api/v1/webhooks/alertmanager"


@pytest.fixture
def token(tmp_path, monkeypatch):
    path = tmp_path / "alertmanager_token" / "webhook_token"
    monkeypatch.setenv("ALERTMANAGER_TOKEN_PATH", str(path))
    get_settings.cache_clear()
    created = ensure_token(path)
    assert created
    yield created
    get_settings.cache_clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    monkeypatch.setattr(engine_alerts, "notify", fake_notify)
    return calls


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _alert(
    name="PaperTicksStale", *, status="firing", severity="warning", fingerprint="fp-1", **extra
):
    return {
        "status": status,
        "labels": {"alertname": name, "severity": severity},
        "annotations": {
            "summary": f"{name} summary",
            "description": "  something\n   went    wrong  ",
        },
        "fingerprint": fingerprint,
        **extra,
    }


def _payload(*alerts, status="firing"):
    return {
        "version": "4",
        "status": status,
        "receiver": "tradingos-backend",
        "alerts": list(alerts),
    }


async def _post(client, body, token_value, *, raw=None):
    return await client.post(
        URL,
        content=raw if raw is not None else json.dumps(body),
        headers={**_auth(token_value), "Content-Type": "application/json"},
    )


async def _rows(db_session_factory):
    async with db_session_factory() as db:
        return (
            (await db.execute(select(EngineAlertEvent).order_by(EngineAlertEvent.opened_at)))
            .scalars()
            .all()
        )


# ---- who may call ----


async def test_a_call_without_the_token_is_refused_and_not_parsed(
    client, db_session_factory, token, sent
):
    no_header = await client.post(
        URL, content="{not json", headers={"Content-Type": "application/json"}
    )
    wrong = await client.post(URL, content="{not json", headers=_auth("wrong-token"))
    wrong_scheme = await client.post(URL, content="{}", headers={"Authorization": f"Basic {token}"})

    assert [r.status_code for r in (no_header, wrong, wrong_scheme)] == [401, 401, 401]
    assert await _rows(db_session_factory) == []


async def test_nothing_is_accepted_when_the_token_does_not_exist(
    client, db_session_factory, token, sent, tmp_path
):
    (tmp_path / "alertmanager_token" / "webhook_token").write_text("", encoding="utf-8")

    resp = await _post(client, _payload(_alert()), token)

    assert resp.status_code == 401


async def test_the_webhook_is_off_when_no_token_path_is_configured(
    client, db_session_factory, monkeypatch, sent
):
    monkeypatch.setenv("ALERTMANAGER_TOKEN_PATH", "")
    get_settings.cache_clear()
    try:
        resp = await client.post(URL, content="{}", headers=_auth("anything"))
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 401


async def test_an_empty_bearer_value_is_refused(client, token, sent):
    resp = await client.post(URL, content="{}", headers={"Authorization": "Bearer "})

    assert resp.status_code == 401


# ---- what is accepted ----


@pytest.mark.parametrize(
    "raw",
    ["{not json", "[]", "{}", '{"alerts": "no"}', '{"alerts": [1, 2]}', "null"],
)
async def test_a_malformed_body_is_a_400(client, token, sent, raw):
    resp = await _post(client, None, token, raw=raw)

    assert resp.status_code == 400


async def test_an_oversized_body_is_refused(client, token, sent):
    resp = await _post(client, None, token, raw=" " * (512 * 1024 + 1))

    assert resp.status_code == 413


async def test_a_firing_alert_opens_an_episode_and_tells_the_engine_channels(
    client, db_session_factory, token, sent
):
    resp = await _post(client, _payload(_alert()), token)

    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "opened": 1, "resolved": 0}
    (row,) = await _rows(db_session_factory)
    assert row.source == "alertmanager"
    assert row.key == "alertmanager:fp-1"
    assert row.kind == "PaperTicksStale"
    assert row.severity == "warning"
    assert row.title == "PaperTicksStale summary"
    assert row.detail == "something went wrong"  # whitespace folded
    assert row.resolved_at is None
    assert row.notified_at is not None
    assert [(c[0].value, c[1], c[2]) for c in sent] == [
        ("engine", "PaperTicksStale summary", "something went wrong")
    ]


async def test_alertmanager_repeating_a_firing_alert_is_the_same_episode(
    client, db_session_factory, token, sent
):
    first = await _post(client, _payload(_alert()), token)
    again = await _post(client, _payload(_alert()), token)

    assert first.json()["opened"] == 1
    assert again.json() == {"received": 1, "opened": 0, "resolved": 0}
    assert len(await _rows(db_session_factory)) == 1
    assert len(sent) == 1


async def test_a_resolved_alert_closes_the_episode_and_says_so(
    client, db_session_factory, token, sent
):
    await _post(client, _payload(_alert()), token)

    resp = await _post(client, _payload(_alert(status="resolved"), status="resolved"), token)

    assert resp.json() == {"received": 1, "opened": 0, "resolved": 1}
    (row,) = await _rows(db_session_factory)
    assert row.resolved_at is not None
    assert row.resolution_notified_at is not None
    assert [c[1] for c in sent] == ["PaperTicksStale summary", "Cleared: PaperTicksStale summary"]


async def test_a_resolved_alert_that_was_never_reported_is_ignored(
    client, db_session_factory, token, sent
):
    resp = await _post(client, _payload(_alert(status="resolved"), status="resolved"), token)

    assert resp.status_code == 200
    assert resp.json()["resolved"] == 0
    assert await _rows(db_session_factory) == []
    assert sent == []


async def test_it_can_fire_again_after_it_resolved(client, db_session_factory, token, sent):
    await _post(client, _payload(_alert()), token)
    await _post(client, _payload(_alert(status="resolved")), token)
    await _post(client, _payload(_alert()), token)

    rows = await _rows(db_session_factory)
    assert [r.resolved_at is None for r in rows] == [False, True]
    assert len(sent) == 3


async def test_alerts_that_arrive_together_are_one_message(client, db_session_factory, token, sent):
    resp = await _post(
        client,
        _payload(
            _alert("PaperTicksStale", fingerprint="a"),
            _alert("MarketHoursBackendDown", severity="critical", fingerprint="b"),
        ),
        token,
    )

    assert resp.json()["opened"] == 2
    assert len(sent) == 1
    assert sent[0][1] == "2 things need attention"
    assert sent[0][3] == {"worst severity": "critical"}


@pytest.mark.parametrize(
    ("label", "stored"),
    [
        ("critical", "critical"),
        ("WARNING", "warning"),
        ("info", "info"),
        ("page", "warning"),
        (None, "warning"),
    ],
)
async def test_severity_comes_from_the_label_and_defaults_to_a_warning(
    client, db_session_factory, token, sent, label, stored
):
    alert = _alert()
    if label is None:
        del alert["labels"]["severity"]
    else:
        alert["labels"]["severity"] = label

    await _post(client, _payload(alert), token)

    (row,) = await _rows(db_session_factory)
    assert row.severity == stored


async def test_an_alert_without_a_fingerprint_is_keyed_by_its_labels(
    client, db_session_factory, token, sent
):
    def bare(name):
        alert = _alert(name)
        del alert["fingerprint"]
        return alert

    await _post(client, _payload(bare("A"), bare("B")), token)
    await _post(client, _payload(bare("A")), token)

    rows = await _rows(db_session_factory)
    assert len(rows) == 2  # A twice is one episode
    assert len({r.key for r in rows}) == 2
    assert all(
        r.key.startswith("alertmanager:") and len(r.key) == len("alertmanager:") + 16 for r in rows
    )


async def test_an_alert_with_no_summary_is_titled_by_its_name(
    client, db_session_factory, token, sent
):
    alert = _alert("DiskFull")
    alert["annotations"] = {}

    await _post(client, _payload(alert), token)

    (row,) = await _rows(db_session_factory)
    assert (row.title, row.detail) == ("DiskFull", "")


async def test_an_alert_with_no_name_still_gets_a_title(client, db_session_factory, token, sent):
    alert = {"status": "firing", "fingerprint": "x", "labels": {}, "annotations": {}}

    await _post(client, _payload(alert), token)

    (row,) = await _rows(db_session_factory)
    assert row.title == "Prometheus alert"


async def test_only_so_many_alerts_are_taken_from_one_call(client, db_session_factory, token, sent):
    alerts = [_alert(f"A{n}", fingerprint=f"fp-{n}") for n in range(205)]

    resp = await _post(client, _payload(*alerts), token)

    assert resp.json() == {"received": 205, "opened": 200, "resolved": 0}


async def test_a_very_long_description_is_cut(client, db_session_factory, token, sent):
    alert = _alert()
    alert["annotations"]["description"] = "word " * 1000

    await _post(client, _payload(alert), token)

    (row,) = await _rows(db_session_factory)
    assert len(row.detail) == 2000
