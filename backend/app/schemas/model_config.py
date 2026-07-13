from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ModelConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    provider_type: str = Field(default="openai_compatible", pattern="^openai_compatible$")
    base_url: HttpUrl
    model_name: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True
    action_protocol: str = Field(default="json_schema", pattern="^(json_schema|json_object|prompt_json|native_tool_call)$")
    structured_output_mode: str = Field(default="json_schema", pattern="^(json_schema|json_object|prompt_json)$")
    request_timeout_seconds: int = Field(default=30, ge=5, le=600)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1.0, ge=0.1, le=60)
    rate_limit_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    context_token_limit: int = Field(default=128000, ge=1024, le=1000000)


class ModelConfigUpdate(ModelConfigWrite):
    api_key: str | None = Field(default=None, max_length=1000)
