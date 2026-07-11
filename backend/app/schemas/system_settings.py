from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator


class ServiceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_url: HttpUrl
    codex_bridge_url: HttpUrl

    @field_validator("runner_url", "codex_bridge_url")
    @classmethod
    def require_local_service(cls, value: HttpUrl) -> HttpUrl:
        if value.host not in {"localhost", "127.0.0.1", "[::1]"}:
            raise ValueError("Only local service endpoints can be changed from the browser.")
        return value
