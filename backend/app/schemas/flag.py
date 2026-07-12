from pydantic import BaseModel, Field


class FlagReviewUpdate(BaseModel):
    review_state: str = Field(pattern="^(OPEN|VALID|INVALID)$")
