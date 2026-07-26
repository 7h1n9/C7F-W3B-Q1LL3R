"""Deterministic policy for moving from confirmation to bounded exploitation."""

from dataclasses import dataclass, field
from typing import Any

METHOD_PHASES = ("DISCOVERY", "CONFIRMATION", "EXPLOITATION", "EXTRACTION", "VERIFICATION", "REPORTING")


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


automation_policy_engine = AutomationPolicyEngine()
