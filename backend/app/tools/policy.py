from urllib.parse import urlparse

from app.core.exceptions import DomainError


def enforce_tool_policy(name: str, arguments: dict, allowed_hosts: list[str]) -> None:
    if name in {"http_request", "http_session_request", "http_extract", "sql_injection_probe", "sql_boolean_compare", "sql_union_probe"}:
        if name == "http_session_request" and str(arguments.get("operation") or "request").lower() in {"inspect", "clear", "create"}:
            return
        host = urlparse(str(arguments.get("url") or arguments.get("endpoint") or "")).hostname
        if not host or host.lower() not in allowed_hosts:
            raise DomainError(
                "TARGET_NOT_ALLOWED",
                "HTTP target host is not in this run's allowlist.",
                {"host": host},
                403,
            )
    if name in {"file_read", "python_run"} and not arguments.get("path"):
        raise DomainError("TOOL_INVALID_ARGUMENT", "A workspace-relative path is required.")
