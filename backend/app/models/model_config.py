from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class ModelConfig(UUIDTimestampMixin, Base):
    __tablename__ = "model_configs"
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048))
    model_name: Mapped[str | None] = mapped_column(String(255))
    encrypted_api_key: Mapped[str | None] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
