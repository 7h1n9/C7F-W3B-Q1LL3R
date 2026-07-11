from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RUNNER_", extra="ignore")
    workspace_root: Path = Path("../data/workspaces")
    max_output_bytes: int = 1_048_576
    job_timeout_seconds: int = 30
    max_upload_bytes: int = 10 * 1024 * 1024
    api_token: str = "development-runner-token"
    pcap_timeout_seconds: int = 20
    pcap_max_fields: int = 12
    pcap_max_limit: int = 500

    environment: str = "development"

    def require_safe_production_token(self) -> None:
        if self.environment.lower() not in {"dev", "development", "test", "testing"} and self.api_token == "development-runner-token":
            raise RuntimeError("Default Runner token is forbidden outside development.")


settings = Settings()
