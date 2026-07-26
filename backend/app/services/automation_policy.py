"""Deterministic policy for moving from confirmation to bounded exploitation."""

from dataclasses import dataclass, field
from typing import Any

METHOD_PHASES = (
    "INFRASTRUCTURE_VALIDATION",
    "DISCOVERY",
    "CONFIRMATION",
    "EXPLOITATION_PLANNING",
    "AUTOMATED_EXPLOITATION",
    "EXTRACTION",
    "VERIFICATION",
    "REPORTING",
)

AUTOMATION_TOOLS_AFTER_SQL_CONFIRMATION = [
    "sql_boolean_compare",
    "request_capture",
    "sqlmap_detect",
    "sqlmap_run",
    "workspace_write_file",
    "sandbox_exec",
    "script_run",
    "python_run",
]


@dataclass(frozen=True)
class AutomationRecommendation:
    tool: str
    arguments: dict[str, Any]
    stop_conditions: list[str] = field(default_factory=list)
    fallback_tool: str | None = None
    failure_pivot: str = "Preserve the failure artifact and switch to the bounded fallback."


class AutomationPolicyEngine:
    """Selects an executable tool once a vulnerability signal is stable."""

    def recommend(
        self,
        vulnerability_class: str,
        manual_probes: list[dict] | None = None,
        response_differences: list[dict] | None = None,
        tool_health: dict[str, bool] | None = None,
        authentication_complexity: str = "low",
        session: dict | None = None,
    ) -> AutomationRecommendation:
        probes = manual_probes or []
        differences = response_differences or []
        health = tool_health or {}
        vuln = vulnerability_class.lower().replace(" ", "_")
        stable = len(differences) >= 2 or any(bool(item.get("stable")) for item in differences)
        if vuln in {"sql", "sqli", "sql_injection", "boolean_sql_injection"} and stable:
            session_state = session or {}
            # Methodology 4.3 keeps the evidence chain explicit. Callers can
            # advance it with these durable session markers; without markers
            # the legacy recommendation remains SQLMap-compatible.
            if not session_state.get("request_captured"):
                return AutomationRecommendation("request_capture", dict(session_state.get("request_capture_arguments") or {}), ["preserve the successful request and its metadata"], "sqlmap_detect")
            if not session_state.get("sqlmap_detected"):
                return AutomationRecommendation("sqlmap_detect", dict(session_state.get("sqlmap_arguments") or {}), ["stop after DBMS and injection point are identified"], "sqlmap_run")
            args = dict((session or {}).get("sqlmap_arguments") or {})
            args.setdefault("action", "detect")
            if health.get("sqlmap_run", True):
                return AutomationRecommendation("sqlmap_run", args, ["stop after target columns are dumped", "do not dump unrelated tables"], "script_run", "SQLMap failure or unsupported encoding -> run the bounded generated script.")
            if health.get("script_run", True):
                return AutomationRecommendation("script_run", dict((session or {}).get("script_arguments") or {}), ["stop after the flag candidate is found"], "sql_boolean_compare", "Script failure -> retain the boolean evidence and return to verification.")
        if vuln in {"sql", "sqli", "sql_injection", "boolean_sql_injection"} and len(probes) >= 3:
            return AutomationRecommendation("sql_boolean_compare", dict((session or {}).get("compare_arguments") or {}), ["stop after a repeatable true/false differential"], "sqlmap_run")
        if health.get("script_run", True):
            return AutomationRecommendation("script_run", dict((session or {}).get("script_arguments") or {}), ["stop when expected artifact exists"], "http_request")
        return AutomationRecommendation("http_request", dict((session or {}).get("verification_arguments") or {}), ["one final calibration request only"], None)

    def recovery_tool(self, vulnerability_class: str = "", original_tool: str = "") -> str:
        vuln = vulnerability_class.lower()
        if "sql" in vuln or original_tool in {"sql_injection_probe", "sql_boolean_compare", "sql_union_probe", "sqlmap_detect"}:
            return "sqlmap_run"
        if original_tool in {"file_read", "file_search"}:
            return "file_read"
        return "script_run"

    @staticmethod
    def automation_required_response() -> dict:
        return {
            "code": "AUTOMATION_REQUIRED",
            "required_stage": "EXPLOITATION_PLANNING",
            "allowed_next_tools": list(AUTOMATION_TOOLS_AFTER_SQL_CONFIRMATION),
        }

    def manual_probe_gate(
        self,
        *,
        vulnerability_confirmed: bool,
        manual_probe_count: int,
        requested_tool: str,
        final_verification: bool = False,
    ) -> dict | None:
        """Return a structured pivot instead of counting a policy rejection as no progress."""
        if vulnerability_confirmed and requested_tool == "http_request" and not final_verification:
            return self.automation_required_response()
        if vulnerability_confirmed and manual_probe_count >= 3 and requested_tool == "http_request":
            return self.automation_required_response()
        return None


class ExploitationPolicyEngine(AutomationPolicyEngine):
    """Provider-neutral policy facade used by OpenAI-compatible and Codex SDK engines."""

    def validate_action(self, *, vulnerability_confirmed: bool, manual_probe_count: int, tool_name: str, final_verification: bool = False) -> dict | None:
        return self.manual_probe_gate(
            vulnerability_confirmed=vulnerability_confirmed,
            manual_probe_count=manual_probe_count,
            requested_tool=tool_name,
            final_verification=final_verification,
        )


automation_policy_engine = AutomationPolicyEngine()
exploitation_policy_engine = ExploitationPolicyEngine()
