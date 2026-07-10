from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RUNNER_", extra="ignore")
    workspace_root: Path = Path("../data/workspaces")
    max_output_bytes: int = 1_048_576
    job_timeout_seconds: int = 30


settings = Settings()
