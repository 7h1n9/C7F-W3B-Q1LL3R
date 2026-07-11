from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")
    database_url: str = "mysql+asyncmy://ctf_agent:ctf_agent@localhost:3306/ctf_agent"
    workspace_root: Path = Path("../data/workspaces")
    runner_url: str = "http://127.0.0.1:8091"
    runner_api_token: str = "development-runner-token"
    codex_bridge_url: str = "http://127.0.0.1:8090"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    encryption_key: str = "development-only-change-me"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def persist_service_urls(runner_url: str, codex_bridge_url: str) -> None:
    """Persist only non-secret local service endpoints for the next restart."""
    path = Path(__file__).resolve().parents[2] / ".env"
    values = {"APP_RUNNER_URL": runner_url, "APP_CODEX_BRIDGE_URL": codex_bridge_url}
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        key = line.partition("=")[0]
        if key in values:
            updated.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
