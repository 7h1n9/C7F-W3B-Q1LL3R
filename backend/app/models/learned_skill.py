from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class LearnedSkillCandidate(UUIDTimestampMixin, Base):
    __tablename__ = "learned_skill_candidates"
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(220), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="QUARANTINED")
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    sanitized_content: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    source_artifact_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_observation_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    security_scan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generalization_score: Mapped[int] = mapped_column(Integer, default=0)


class LearnedSkillCandidateSource(UUIDTimestampMixin, Base):
    __tablename__ = "learned_skill_candidate_sources"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learned_skill_candidates.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class LearnedSkillReview(UUIDTimestampMixin, Base):
    __tablename__ = "learned_skill_reviews"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learned_skill_candidates.id"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(120), default="human")
    review_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class LearnedSkillValidationRun(UUIDTimestampMixin, Base):
    __tablename__ = "learned_skill_validation_runs"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learned_skill_candidates.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
