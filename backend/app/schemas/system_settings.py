from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from app.core.config import validate_service_host


class ServiceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Optional now that the legacy Runner is retired: a caller that only wants to
    # set the global default model has no Runner address to send, and requiring
    # one would make the retired service a prerequisite for an unrelated setting.
    # When absent the stored value is kept as-is.
    runner_url: HttpUrl | None = None
    # Existence/enabled checks need the DB, so they live in the route rather than
    # here; the schema only defines the shape. None clears the global default.
    default_model_config_id: str | None = None

    @model_validator(mode="after")
    def validate_endpoints(self) -> "ServiceSettingsUpdate":
        if self.runner_url is not None:
            validate_service_host(self.runner_url.host, allow_private_runner=True)
        # An empty string from the UI means "cleared", not "a config with an empty
        # id" -- normalise it so callers see one representation of "unset".
        if self.default_model_config_id == "":
            self.default_model_config_id = None
        return self