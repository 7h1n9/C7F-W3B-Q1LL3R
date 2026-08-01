"""Authoritative Runner tool registry.

The execution backend and the capability endpoint must describe the same
registry.  Keeping the names in ``main.py`` and the handlers in a second
mapping allowed a tool to appear healthy without being executable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    implemented: bool = True
    supported_dbms: tuple[str, ...] = ()
    placeholder: bool = False


def _registrations() -> tuple[ToolRegistration, ...]:
    names = (
        "http_request", "http_session_request", "http_extract", "whatweb_fingerprint",
        "js_asset_analyze", "source_map_analyze", "file_type", "strings_extract",
        "archive_list", "content_discovery", "jwt_inspect", "session_inspect",
        "session_list_secret_refs", "jwt_clone_claims", "jwt_sign",
        "http_session_set_cookie_ref", "file_read", "file_search", "python_run",
        "script_run", "sandbox_exec", "pcap_metadata", "pcap_protocols", "pcap_query",
        "pcap_tcp_stream", "pcap_http_objects", "pcap_dns_summary", "pcap_credentials",
        "request_capture", "sqlmap_detect", "sqlmap_run", "sql_injection_probe",
        "sql_boolean_compare", "sql_union_probe", "oracle_probe_matrix",
        "boolean_config_extract", "oracle_expression_calibration", "mysql_metadata_discovery", "sqlite_metadata_discovery",
        "nikto_scan", "binwalk_scan", "exiftool_metadata",
    )
    placeholders = {
        "pcap_tcp_stream", "pcap_http_objects", "pcap_dns_summary", "pcap_credentials",
        "nmap_service_probe", "nikto_scan", "binwalk_scan", "exiftool_metadata",
    }
    registrations = [
        ToolRegistration(
            name=name,
            implemented=name not in placeholders,
            supported_dbms=("mysql",) if name in {"mysql_metadata_discovery", "boolean_config_extract", "oracle_expression_calibration"} else (),
            placeholder=name in placeholders,
        )
        for name in names
    ]
    registrations.append(ToolRegistration("nmap_service_probe", implemented=False, placeholder=True))
    return tuple(registrations)


TOOL_REGISTRY = _registrations()
TOOL_REGISTRATIONS = {item.name: item for item in TOOL_REGISTRY}


def registration(name: str) -> ToolRegistration:
    try:
        return TOOL_REGISTRATIONS[name]
    except KeyError as error:
        raise KeyError(f"Runner tool is not registered: {name}") from error
