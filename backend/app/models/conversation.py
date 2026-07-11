from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class ChallengeConversation(UUIDTimestampMixin, Base):
    __tablename__ = "challenge_conversations"
    challenge_id: Mapped[str] = mapped_column(
        ForeignKey("challenges.id"), nullable=False, index=True
    )
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ChallengeConversationSkill(Base):
    __tablename__ = "challenge_conversation_skills"
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("challenge_conversations.id"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class ChallengeMessage(UUIDTimestampMixin, Base):
    __tablename__ = "challenge_messages"
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("challenge_conversations.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="COMPLETED")
    usage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
