from app.core.exceptions import DomainError
from app.services.target_allowlist import require_target_allowed


def enforce_tool_policy(name: str, arguments: dict, allowed_hosts: list[str]) -> None:
    if name in {"http_request", "http_session_request", "http_extract", "sql_injection_probe", "sql_boolean_compare", "sql_union_probe", "oracle_probe_matrix", "boolean_config_extract", "sqlite_metadata_discovery", "request_capture"}:
        if name == "http_session_request" and str(arguments.get("operation") or "request").lower() in {"inspect", "clear", "create"}:
            return
        if name == "request_capture" and arguments.get("request_file") and not arguments.get("url"):
            return
        request = arguments.get("request") if isinstance(arguments.get("request"), dict) else {}
        candidate_url = arguments.get("url") or arguments.get("endpoint") or request.get("url")
        if not candidate_url:
            raise DomainError("TOOL_INVALID_ARGUMENT", "HTTP target URL is required.", status_code=422)
        require_target_allowed(str(candidate_url), allowed_hosts)
    if name in {"file_read", "python_run"} and not arguments.get("path"):
        raise DomainError("TOOL_INVALID_ARGUMENT", "A workspace-relative path is required.")
