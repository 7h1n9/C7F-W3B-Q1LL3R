from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from app.core.config import validate_service_host


class ServiceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runner_url: HttpUrl

    @model_validator(mode="after")
    def validate_endpoints(self) -> "ServiceSettingsUpdate":
        validate_service_host(self.runner_url.host, allow_private_runner=True)
        return self