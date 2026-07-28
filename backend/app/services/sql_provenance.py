"""Evidence provenance gates for SQL expressions."""

from __future__ import annotations

from app.core.exceptions import DomainError

REQUIRED_PROVENANCE = {
    "target_expression", "expression_type", "supporting_evidence_ids",
    "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id",
    "assumption_status",
}
EXPRESSION_TYPES = {"METADATA_DISCOVERY", "VALUE_EXTRACTION", "FLAG_SEARCH"}


def validate_sql_expression_provenance(arguments: dict, *, required: bool = True, verified_schema: bool = False) -> dict:
    if not required and not arguments.get("target_expression"):
        return {}
    missing = sorted(key for key in REQUIRED_PROVENANCE if key not in arguments)
    if missing:
        raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", "SQL expression provenance is required before execution.", {"missing_fields": missing}, 422)
    value = {key: arguments.get(key) for key in REQUIRED_PROVENANCE}
    if not str(value["target_expression"] or "").strip() or value["expression_type"] not in EXPRESSION_TYPES:
        raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", "SQL expression or expression_type is invalid.", status_code=422)
    if value["assumption_status"] not in {"VERIFIED", "HYPOTHESIS"}:
        raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", "assumption_status must be VERIFIED or HYPOTHESIS.", status_code=422)
    for key in ("supporting_evidence_ids", "supporting_fact_ids"):
        if not isinstance(value[key], list) or not value[key] or not all(str(item).strip() for item in value[key]):
            raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", f"{key} must contain source identifiers.", status_code=422)
    if not str(value["source_hypothesis_id"] or "").strip() or not str(value["approved_analysis_review_id"] or "").strip():
        raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", "Hypothesis and Analysis review sources are required.", status_code=422)
    # The common ungrounded answer leak is rejected even when a caller tries
    # to hide it inside a larger expression.
    normalized = " ".join(str(value["target_expression"]).lower().split())
    if "select value from config" in normalized and (value["assumption_status"] != "VERIFIED" or not verified_schema):
        raise DomainError("SQL_EXPRESSION_PROVENANCE_REQUIRED", "config.value cannot be used without verified schema evidence.", status_code=422)
    return value
