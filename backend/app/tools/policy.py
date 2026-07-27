from urllib.parse import urlparse

from app.core.exceptions import DomainError


def enforce_tool_policy(name: str, arguments: dict, allowed_hosts: list[str]) -> None:
    if name in {"http_request", "http_session_request", "http_extract", "sql_injection_probe", "sql_boolean_compare", "sql_union_probe", "oracle_probe_matrix", "boolean_config_extract", "request_capture"}:
        if name == "http_session_request" and str(arguments.get("operation") or "request").lower() in {"inspect", "clear", "create"}:
            return
        if name == "request_capture" and arguments.get("request_file") and not arguments.get("url"):
            return
        request = arguments.get("request") if isinstance(arguments.get("request"), dict) else {}
        candidate_url = arguments.get("url") or arguments.get("endpoint") or request.get("url")
        host = urlparse(str(candidate_url or "")).hostname
        if not host or host.lower() not in allowed_hosts:
            raise DomainError(
                "TARGET_NOT_ALLOWED",
                "HTTP target host is not in this run's allowlist.",
                {"host": host},
                403,
            )
    if name in {"file_read", "python_run"} and not arguments.get("path"):
        raise DomainError("TOOL_INVALID_ARGUMENT", "A workspace-relative path is required.")
