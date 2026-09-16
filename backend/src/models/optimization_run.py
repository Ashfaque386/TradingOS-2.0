import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class OptimizationRun(Base):
    """One Optuna hyperparameter sweep (src/engine/optimization/
    hyperparameter_sweep.py, Build Spec §10) against a StrategyVersion.
    Every trial in the sweep was a real, complete backtest -- best_value
    and param_importance are read off the real completed Optuna study,
    never estimated.
    """

    __tablename__ = "optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    objective_metric: Mapped[str] = mapped_column(String(32), nullable=False)
    n_trials: Mapped[int] = mapped_column(Integer, nullable=False)
    best_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    best_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    param_importance: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"OptimizationRun(id={self.id!r}, n_trials={self.n_trials!r})"
