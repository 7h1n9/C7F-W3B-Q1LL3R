from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
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
    action_protocol: Mapped[str] = mapped_column(String(30), default="json_schema")
    structured_output_mode: Mapped[str] = mapped_column(String(30), default="json_schema")
    supports_json_schema: Mapped[bool | None] = mapped_column(Boolean)
    supports_json_object: Mapped[bool | None] = mapped_column(Boolean)
    supports_native_tool_call: Mapped[bool | None] = mapped_column(Boolean)
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    retry_base_seconds: Mapped[float] = mapped_column(Float, default=1.0)
    rate_limit_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    context_token_limit: Mapped[int] = mapped_column(Integer, default=128000)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    capabilities_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
