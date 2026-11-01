"""Prometheus -> Alertmanager -> the backend's alert webhook, as configured in the repo.

Nothing here starts a container. It checks that the pieces name each other correctly -- the URL
Alertmanager posts to is a real route, the token file it reads is where the backend writes it, the
rule's metric exists -- and that Alertmanager stays off the host network and out of the secrets.
"""

from pathlib import Path

import yaml

from src.core.config import Settings
from src.main import app
from src.observability.metrics import render_latest_metrics

REPO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
ALERTMANAGER = yaml.safe_load(
    (REPO / "observability" / "alertmanager" / "alertmanager.yml").read_text(encoding="utf-8")
)
PROMETHEUS = yaml.safe_load(
    (REPO / "observability" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
)
RULES = yaml.safe_load(
    (REPO / "observability" / "prometheus" / "alert_rules.yml").read_text(encoding="utf-8")
)
APP_DIR = Path("/app")
TOKEN_SETTING = Settings.model_fields["alertmanager_token_path"].default


def _mounts(service: str) -> dict[str, str]:
    mounts = {}
    for entry in COMPOSE["services"][service]["volumes"]:
        source, target, *mode = str(entry).split(":")
        mounts[target] = source + (":" + mode[0] if mode else "")
    return mounts


def test_alertmanager_is_pinned_and_not_published_to_the_host():
    service = COMPOSE["services"]["alertmanager"]

    assert "ports" not in service  # Prometheus and the backend reach it over the stack's network
    image, _, tag = service["image"].partition(":")
    assert image == "prom/alertmanager"
    assert tag and tag != "latest"


def test_the_token_lives_on_a_volume_of_its_own_that_alertmanager_can_only_read():
    token_dir = (APP_DIR / Path(TOKEN_SETTING)).parent.as_posix()
    secrets_dir = (APP_DIR / Path(Settings.model_fields["secrets_store_path"].default)).parent
    backend = _mounts("backend")

    volume = backend[token_dir]
    assert volume in COMPOSE["volumes"]
    assert token_dir != secrets_dir.as_posix()
    assert backend[secrets_dir.as_posix()] != volume  # not the volume holding the encrypted stores
    assert _mounts("alertmanager")["/etc/alertmanager/token"] == volume + ":ro"
    # The only thing Alertmanager sees of the backend's data is that one token.
    assert not any(
        "secrets" in source or "pgdata" in source for source in _mounts("alertmanager").values()
    )


def test_alertmanager_posts_to_a_real_route_with_the_token_the_backend_writes():
    [receiver] = ALERTMANAGER["receivers"]
    [hook] = receiver["webhook_configs"]
    posts = {path for path, ops in app.openapi()["paths"].items() if "post" in ops}

    assert hook["url"] == "http://backend:8000/api/v1/webhooks/alertmanager"
    assert hook["url"].removeprefix("http://backend:8000") in posts
    assert hook["send_resolved"] is True  # what lets the backend say "cleared"
    auth = hook["http_config"]["authorization"]
    assert auth["type"] == "Bearer"
    assert auth["credentials_file"] == "/etc/alertmanager/token/" + Path(TOKEN_SETTING).name
    assert ALERTMANAGER["route"]["receiver"] == receiver["name"]


def test_prometheus_sends_its_alerts_to_the_alertmanager_service():
    [manager] = PROMETHEUS["alerting"]["alertmanagers"]

    assert manager["static_configs"] == [{"targets": ["alertmanager:9093"]}]
    assert "alertmanager" in COMPOSE["services"]
    assert "alertmanager" in COMPOSE["services"]["prometheus"]["depends_on"]
    assert "9093" in " ".join(COMPOSE["services"]["alertmanager"]["healthcheck"]["test"])


def test_the_backend_is_told_where_alertmanager_is_and_the_new_tunables_reach_it():
    env = COMPOSE["services"]["backend"]["environment"]

    assert env["ALERTMANAGER_URL"].startswith("${ALERTMANAGER_URL:-http://alertmanager:9093}")
    for name in (
        "PAPER_STALE_PRICE_SECONDS",
        "ENGINE_ALERT_CONFIRM_SECONDS",
        "PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS",
    ):
        assert name in env, f"{name} is a setting but is not forwarded to the container"


def test_the_watcher_rule_reads_a_metric_the_backend_really_exposes():
    rules = [r for group in RULES["groups"] for r in group["rules"]]
    [stalled] = [r for r in rules if r["alert"] == "EngineAlertWatcherStalled"]
    exposed = render_latest_metrics()[0].decode()

    assert stalled["labels"]["severity"] == "critical"
    metric = "tradingos_engine_alert_watch_last_success_timestamp_seconds"
    assert metric in stalled["expr"]
    assert f"# TYPE {metric} gauge" in exposed
    assert "# TYPE tradingos_engine_alerts_open gauge" in exposed


def test_the_backup_rules_read_a_gauge_the_backend_really_exposes():
    rules = {r["alert"]: r for group in RULES["groups"] for r in group["rules"]}
    exposed = render_latest_metrics()[0].decode()

    assert rules["PostgresBackupStale"]["labels"]["severity"] == "critical"
    assert rules["LakeBackupStale"]["labels"]["severity"] == "warning"
    assert 'tradingos_backup_stale{kind="postgres"}' in rules["PostgresBackupStale"]["expr"]
    assert 'tradingos_backup_stale{kind="lake"}' in rules["LakeBackupStale"]["expr"]
    assert "# TYPE tradingos_backup_stale gauge" in exposed
    assert "# TYPE tradingos_backup_newest_ok_age_seconds gauge" in exposed


def test_every_alert_rule_carries_a_severity_the_webhook_understands():
    rules = [r for group in RULES["groups"] for r in group["rules"]]

    assert rules
    for rule in rules:
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
