from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class Challenge(UUIDTimestampMixin, Base):
    __tablename__ = "challenges"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    flag_pattern: Mapped[str] = mapped_column(String(500), default=r"flag\{[^}]+\}")
    source_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
