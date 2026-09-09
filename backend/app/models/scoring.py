from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class ChallengePrediction(UUIDTimestampMixin, Base):
    __tablename__ = "challenge_predictions"

    challenge_id: Mapped[str] = mapped_column(
        ForeignKey("challenges.id"), nullable=False, unique=True, index=True
    )
    prediction_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    predicted_solve_seconds: Mapped[int | None] = mapped_column(Integer)
    predicted_tokens: Mapped[int | None] = mapped_column(Integer)
    predicted_tool_calls: Mapped[int | None] = mapped_column(Integer)
    difficulty: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[float | None] = mapped_column(Float)
    rationale_zh: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    usage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunScore(UUIDTimestampMixin, Base):
    __tablename__ = "run_scores"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("solve_runs.id"), nullable=False, unique=True, index=True
    )
    challenge_id: Mapped[str] = mapped_column(
        ForeignKey("challenges.id"), nullable=False, index=True
    )
    prediction_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actual_seconds: Mapped[float | None] = mapped_column(Float)
    actual_tokens: Mapped[int | None] = mapped_column(Integer)
    solved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    time_ratio: Mapped[float | None] = mapped_column(Float)
    token_ratio: Mapped[float | None] = mapped_column(Float)
    time_points: Mapped[float | None] = mapped_column(Float)
    token_points: Mapped[float | None] = mapped_column(Float)
    solved_points: Mapped[float | None] = mapped_column(Float)
    total_score: Mapped[float | None] = mapped_column(Float)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    score_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    error_code: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
