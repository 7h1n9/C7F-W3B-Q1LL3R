from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class Challenge(UUIDTimestampMixin, Base):
    __tablename__ = "challenges"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    challenge_type: Mapped[str] = mapped_column(String(40), default="WEB_TARGET")
    target_url: Mapped[str | None] = mapped_column(String(2048))
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    flag_pattern: Mapped[str] = mapped_column(String(500), default=r"flag\{[^}]+\}")
    source_path: Mapped[str | None] = mapped_column(String(1024))
    primary_attachment_id: Mapped[str | None] = mapped_column(
        ForeignKey("challenge_attachments.id")
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ChallengeAttachment(UUIDTimestampMixin, Base):
    __tablename__ = "challenge_attachments"
    challenge_id: Mapped[str] = mapped_column(
        ForeignKey("challenges.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="OTHER")
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
