from pydantic import BaseModel, Field

from app.schemas.run import RunCreate


class ConversationCreate(BaseModel):
    model_config_id: str | None = None
    title: str = Field(default="新对话", min_length=1, max_length=200)
    skill_ids: list[str] = Field(default_factory=list, max_length=30)


class ConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=12000)


class ConversationRunCreate(RunCreate):
    pass
