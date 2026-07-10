from pydantic import BaseModel, Field


class ToolInvoke(BaseModel):
    name: str = Field(pattern="^(http_request|file_read|file_search|python_run)$")
    arguments: dict
