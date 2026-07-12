from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class Skill(UUIDTimestampMixin, Base):
    __tablename__ = "skills"
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="CUSTOM")
    skill_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="SPECIALIST")
    activation_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="MANUAL")
    triggers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    prerequisites: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recommended_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    forbidden_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    ctf_phases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    challenge_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_level: Mapped[str] = mapped_column(String(20), default="low")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin_path: Mapped[str | None] = mapped_column(String(512), unique=True)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ModelSkillBinding(UUIDTimestampMixin, Base):
    __tablename__ = "model_skill_bindings"
    __table_args__ = (
        UniqueConstraint("model_config_id", "skill_id", name="uq_model_skill_binding"),
    )
    model_config_id: Mapped[str] = mapped_column(ForeignKey("model_configs.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ChallengeSkillBinding(Base):
    __tablename__ = "challenge_skill_bindings"
    challenge_id: Mapped[str] = mapped_column(ForeignKey("challenges.id"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class RunSkillSnapshot(UUIDTimestampMixin, Base):
    __tablename__ = "run_skill_snapshots"
    run_id: Mapped[str] = mapped_column(ForeignKey("solve_runs.id"), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    skill_name: Mapped[str] = mapped_column(String(120), nullable=False)
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools_snapshot: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=100)
