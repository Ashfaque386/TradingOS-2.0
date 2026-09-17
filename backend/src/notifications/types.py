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
    """The exact four alert levels the frontend's Settings > Notification
    Channels screen already exposes (app/settings/page.tsx's ALERT_LEVELS
    constant, built ahead of this phase) -- this is the closed, authoritative
    set of "visual-only alert concepts" this phase gives a real outbound
    equivalent to, not an open-ended list this module invents on its own.
    """

    KILL_SWITCH = "kill-switch"
    SIGN_OFF = "sign-off"
    GO_LIVE = "go-live"
    DAILY = "daily"
