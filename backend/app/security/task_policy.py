"""Phase-scoped action policy for vulnerability task planning.

This module is deliberately smaller than the lifecycle and decision layers.
It does not infer vulnerabilities or inspect evidence; it only answers which
tool families are admissible for a known vulnerability type and phase.
"""

from __future__ import annotations

from typing import Any


_SQL_INJECTION = "SQL_INJECTION"

# This is the controller's proposal vocabulary.  The policy may mention
# future semantic actions (for example ``report``) without making them
# executable by itself.
_KNOWN_ACTIONS = (
    "http_request",
    "content_discovery",
    "sql_boolean_compare",
    "oracle_probe_matrix",
    "mysql_metadata_discovery",
    "sql_extract",
    "impact_validation",
    "report",
    "boolean_config_extract",
    "script_run",
    "http_compare",
)

_SQL_PHASES: dict[str, tuple[str, ...]] = {
    "SURFACE_ANALYSIS": ("content_discovery", "http_request"),
    "BASELINE": ("http_request",),
    "VALIDATION": ("sql_boolean_compare", "oracle_probe_matrix"),
    "EXPLOITATION": ("mysql_metadata_discovery", "sql_extract"),
    "IMPACT": ("impact_validation",),
    "REPORTING": ("report",),
}

_PHASE_ALIASES = {
    "INTAKE": "SURFACE_ANALYSIS",
    "RECON": "SURFACE_ANALYSIS",
    "HYPOTHESIS": "SURFACE_ANALYSIS",
    "SURFACE": "SURFACE_ANALYSIS",
    "BASELINE": "BASELINE",
    "BUSINESS_BASELINE": "BASELINE",
    "MAPPING": "VALIDATION",
    "ORACLE": "VALIDATION",
    "BOOLEAN_ORACLE": "VALIDATION",
    "VALIDATION": "VALIDATION",
    "EXPLOIT": "EXPLOITATION",
    "EXPLOITATION": "EXPLOITATION",
    "IMPACT": "IMPACT",
    "REPORT": "REPORTING",
    "REPORTING": "REPORTING",
}


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_")


def _canonical_phase(phase: Any) -> str:
    normalized = _normalize(phase)
    return _PHASE_ALIASES.get(normalized, normalized)


def get_allowed_tools(vulnerability_type: Any, phase: Any) -> dict[str, Any]:
    """Return the action policy for a vulnerability type and phase.

    Unknown vulnerability types remain unrestricted by this layer so legacy
    and non-SQL flows retain their existing behavior.
    """

    normalized_type = _normalize(vulnerability_type)
    canonical_phase = _canonical_phase(phase)
    if normalized_type != _SQL_INJECTION or canonical_phase not in _SQL_PHASES:
        return {
            "phase": canonical_phase,
            "allowed_tools": [],
            "forbidden_tools": [],
        }
    allowed = list(_SQL_PHASES[canonical_phase])
    return {
        "phase": canonical_phase,
        "allowed_tools": allowed,
        "forbidden_tools": [tool for tool in _KNOWN_ACTIONS if tool not in allowed],
    }


def validate_tools(vulnerability_type: Any, phase: Any, tools: list[str] | tuple[str, ...] | None) -> dict[str, Any]:
    """Validate a Planner/ApprovedAction tool set against the phase policy."""

    policy = get_allowed_tools(vulnerability_type, phase)
    requested = [str(tool) for tool in (tools or [])]
    allowed = set(policy["allowed_tools"])
    invalid = [tool for tool in requested if allowed and tool not in allowed]
    if not policy["allowed_tools"]:
        return {"decision": "APPROVE", "reason": "NO_POLICY", "invalid_tools": [], "policy": policy}
    if not requested:
        return {"decision": "REVISE", "reason": "NO_ACTION_DECLARED", "invalid_tools": [], "policy": policy}
    if invalid:
        return {
            "decision": "REVISE",
            "reason": f"{policy['phase']} phase only allows: {', '.join(policy['allowed_tools'])}",
            "invalid_tools": invalid,
            "policy": policy,
        }
    return {"decision": "APPROVE", "reason": "POLICY_COMPLIANT", "invalid_tools": [], "policy": policy}


def vulnerability_type_from_metadata(metadata: dict[str, Any] | None) -> str:
    return _normalize((metadata or {}).get("vulnerability_type"))
