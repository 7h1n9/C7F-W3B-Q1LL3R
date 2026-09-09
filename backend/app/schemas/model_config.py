from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max", "ultra"]
WireApi = Literal["responses", "chat_completions"]


class ModelConfigWrite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    provider_type: Literal["openai_compatible", "codex_cli"] = "openai_compatible"
    base_url: HttpUrl | None = None
    wire_api: WireApi | None = None
    model_name: str = Field(min_length=1, max_length=255, pattern=r"^\S+$")
    reasoning_effort: ReasoningEffort | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True
    roles: list[Literal["worker", "coordinator_reason"]] = Field(default_factory=lambda: ["worker"], max_length=2)
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

    @model_validator(mode="after")
    def validate_provider_contract(self) -> "ModelConfigWrite":
        if self.provider_type == "openai_compatible" and self.base_url is None:
            raise ValueError("openai_compatible model requires base_url")
        if self.provider_type == "codex_cli" and any(role != "worker" for role in self.roles):
            raise ValueError("codex_cli models are Worker-only")
        if self.provider_type == "codex_cli" and self.reasoning_effort is None:
            self.reasoning_effort = "medium"
        if self.provider_type == "codex_cli":
            if self.base_url is not None and self.api_key is None:
                raise ValueError("codex_cli API endpoint requires api_key")
            if self.base_url is not None and self.wire_api is None:
                self.wire_api = "responses"
        else:
            self.wire_api = None
        return self


class ModelConfigUpdate(ModelConfigWrite):
    api_key: str | None = Field(default=None, max_length=1000)
