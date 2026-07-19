import ipaddress
import os
import socket
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
    # Shared only with the local run-scoped ctfctl MCP subprocess. It is not
    # included in browser responses or model context.
    ctfctl_internal_access_key: str = "development-ctfctl-access-key"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    encryption_key: str = "development-only-change-me"
    allowed_service_cidrs: str = "127.0.0.0/8,192.168.56.0/24,192.168.236.0/24"
    environment: str = "development"
    # Local Kali/WebSocket setups may intentionally route localhost targets
    # through a tunnel. Keep the production default blocked unless explicitly
    # enabled in the environment.
    allow_remote_local_targets: bool = False
    codex_diagnostic_mode: bool = False
    historical_lesson_mode: str = "strategy_only"

    def require_safe_production_secrets(self) -> None:
        if self.environment.lower() not in {"dev", "development", "test", "testing"} and (
            self.runner_api_token == "development-runner-token"
            or self.encryption_key == "development-only-change-me"
        ):
            raise RuntimeError(
                "Default Runner token or encryption key is forbidden outside development."
            )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def codex_diagnostics_enabled(self) -> bool:
        raw = os.getenv("CODEX_DIAGNOSTIC_MODE")
        if raw is not None:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(self.codex_diagnostic_mode)


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


def validate_service_host(host: str, allow_private_runner: bool) -> None:
    if host == "localhost":
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as error:
        raise ValueError("Service hostname could not be resolved.") from error
    networks = [
        ipaddress.ip_network(item.strip())
        for item in get_settings().allowed_service_cidrs.split(",")
        if item.strip()
    ]
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if allow_private_runner and any(ip in network for network in networks):
            continue
        if not allow_private_runner and ip.is_loopback:
            continue
        raise ValueError("Service address is outside the allowed private network.")
