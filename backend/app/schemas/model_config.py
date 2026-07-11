from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ModelConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    provider_type: str = Field(default="openai_compatible", pattern="^openai_compatible$")
    base_url: HttpUrl
    model_name: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True


class ModelConfigUpdate(ModelConfigWrite):
    api_key: str | None = Field(default=None, max_length=1000)
