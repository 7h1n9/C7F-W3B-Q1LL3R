from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")
    database_url: str = "mysql+asyncmy://ctf_agent:ctf_agent@localhost:3306/ctf_agent"
    workspace_root: Path = Path("../data/workspaces")
    runner_url: str = "http://127.0.0.1:8091"
    codex_bridge_url: str = "http://127.0.0.1:8090"
    cors_origins: str = "http://localhost:5173"
    encryption_key: str = "development-only-change-me"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
