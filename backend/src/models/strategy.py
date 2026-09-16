import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class StrategyStatus(StrEnum):
    IDEATION = "Ideation"
    CODING = "Coding"
    BACKTESTING = "Backtesting"
    PAPER_TRADING = "PaperTrading"
    LIVE_ELIGIBLE = "LiveEligible"
    LIVE = "Live"
    DEPRECATED = "Deprecated"


class InstrumentClass(StrEnum):
    EQUITY = "equity"
    OPTIONS = "options"


class Strategy(Base, TimestampMixin):
    """One strategy's lifecycle record (Build Spec §9). `status` is
    deliberately a plain `String` column plus an explicit DB-level
    `CheckConstraint` -- NOT a native Postgres enum type like most other
    status columns in this codebase (Task.status, OrganizationRun.status,
    etc). That's a conscious choice here, not an oversight: it still gives
    the same DB-level guarantee (an invalid status string cannot be
    inserted, full stop -- proven by a test that attempts a raw INSERT with
    a bad value and confirms Postgres itself rejects it), while sidestepping
    the native-enum-type-not-dropped-on-downgrade defect class that has hit
    this project's Alembic migrations three times already (Phases 0-2) --
    there's no separate CREATE TYPE/DROP TYPE lifecycle to get wrong.

    The current code (`StrategyVersion.code`) isn't referenced from here --
    no `current_version_id` column -- callers query
    `MAX(strategy_versions.version_number)` for a given `strategy_id`
    instead, the same "no denormalized pointer" choice Phase 3's
    PromptVersion made for its own ACTIVE version.
    """

    __tablename__ = "strategies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Ideation','Coding','Backtesting','PaperTrading',"
            "'LiveEligible','Live','Deprecated')",
            name="ck_strategies_status",
        ),
        CheckConstraint(
            "instrument_class IN ('equity','options')", name="ck_strategies_instrument_class"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_class: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InstrumentClass.EQUITY.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyStatus.IDEATION.value
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    def __repr__(self) -> str:
        return f"Strategy(id={self.id!r}, name={self.name!r}, status={self.status!r})"
