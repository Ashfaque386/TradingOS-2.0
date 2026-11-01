"""Shared types for the notifications package (Build Spec §18). Kept
dependency-free (no DB/httpx imports) so every other module here -- and
the channel/inbound/outbound modules that import from it -- can share one
definition of "which channel" and "which alert level" without a cycle.
"""

from enum import StrEnum


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"


class AlertLevel(StrEnum):
    """The closed set of alert levels a channel can subscribe to (the frontend's Settings >
    Notification Channels screen offers exactly these: lib/api.ts's ALERT_LEVELS). Not an
    open-ended list a module invents on its own.

    `ENGINE` is for the trading engine itself needing a person: a position nobody is watching, a
    price feed that has stopped, a stop-loss that fired, a futures roll or an expiry square-off
    that could not be done, a broker that refused the stored credentials, and the infrastructure
    alerts Prometheus raises through Alertmanager (src.orchestration.engine_alerts).
    """

    KILL_SWITCH = "kill-switch"
    SIGN_OFF = "sign-off"
    GO_LIVE = "go-live"
    DAILY = "daily"
    ENGINE = "engine"
