import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class EngineAlertEvent(Base):
    """One thing the trading engine needed a person for, from the moment it was seen until it
    cleared (src.orchestration.engine_alerts).

    An *episode*: it opens when the condition is first seen and resolves when it is gone, and a
    condition that comes back later is a new row, so "one alert per episode" and "alert again on
    recurrence" are the same rule. `key` names the condition (`<subscription id>:unmanaged`,
    `roll:<subscription id>`, `alertmanager:<fingerprint>`); at most one row per key is open.

    `source` says who raised it: `watcher` (the paper-desk conditions, checked every minute),
    `event` (something the engine did or failed to do at a moment: a stop-loss exit, a roll
    that was postponed, a broker that refused the credentials) or `alertmanager` (a Prometheus
    rule). A stop-loss exit is over the instant it happens, so it opens and resolves at once.

    `notified_at` is when the opening notice went out to the subscribed channels. It stays empty
    if the condition cleared before it had lasted long enough to be worth a message, and
    `resolution_notified_at` is the "it has cleared" notice, sent only for an episode that was
    announced.
    """

    __tablename__ = "engine_alert_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('watcher','event','alertmanager')", name="ck_engine_alert_events_source"
        ),
        CheckConstraint(
            "severity IN ('critical','warning','info')", name="ck_engine_alert_events_severity"
        ),
        # One open episode per condition. Partial, so a resolved episode never blocks the next.
        Index(
            "uq_engine_alert_events_open_key",
            "key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_engine_alert_events_opened_at", "opened_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("paper_trading_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"EngineAlertEvent(key={self.key!r}, severity={self.severity!r})"
