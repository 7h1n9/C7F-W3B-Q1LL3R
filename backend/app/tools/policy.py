from urllib.parse import urlparse

from app.core.exceptions import DomainError


def enforce_tool_policy(name: str, arguments: dict, allowed_hosts: list[str]) -> None:
    if name == "http_request":
        host = urlparse(str(arguments.get("url", ""))).hostname
        if not host or host.lower() not in allowed_hosts:
            raise DomainError(
                "TARGET_NOT_ALLOWED",
                "HTTP target host is not in this run's allowlist.",
                {"host": host},
                403,
            )
    if name in {"file_read", "python_run"} and not arguments.get("path"):
        raise DomainError("TOOL_INVALID_ARGUMENT", "A workspace-relative path is required.")
