"""Deterministic stage selection from durable evidence."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageDecision:
    stage: str
    requires_user: bool = False
    terminal_reason: str | None = None
    reason: str = ""
    details: dict = field(default_factory=dict)


class StageDecider:
    METADATA_FACTS = {
        "asset_warranty.mysql_version",
        "asset_warranty.mysql_version_comment",
        "asset_warranty.current_database",
        "asset_warranty.mysql_user_tables",
        "asset_warranty.mysql_candidate_columns",
    }

    def decide(self, *, asset_warranty_mysql: bool, verified_fact_keys: set[str],
               capability_ledger: dict | None = None, candidate_exists: bool = False,
               declared_fields: set[str] | None = None, tested_fields: set[str] | None = None) -> StageDecision:
        if not asset_warranty_mysql:
            return StageDecision("FLAG_VERIFICATION" if candidate_exists else "HYPOTHESIS")
        if "asset_warranty.valid_baseline" not in verified_fact_keys or "asset_warranty.invalid_baseline" not in verified_fact_keys:
            return StageDecision("BUSINESS_BASELINE")
        if "asset_warranty.mysql_boolean_oracle" not in verified_fact_keys:
            declared = set(declared_fields or ())
            tested = set(tested_fields or ())
            if declared and declared <= tested:
                return StageDecision(
                    "REPORTING",
                    terminal_reason="MYSQL_PREDICATE_NOT_CONFIRMED",
                    reason="All declared business fields were tested without a stable Boolean Oracle.",
                    details={"tested_fields": sorted(tested), "declared_fields": sorted(declared)},
                )
            return StageDecision("BOOLEAN_ORACLE")
        if "asset_warranty.oracle_calibration_matrix" not in verified_fact_keys or "asset_warranty.mysql_dbms" not in verified_fact_keys:
            return StageDecision("ORACLE_CALIBRATION")
        if not self.METADATA_FACTS <= verified_fact_keys:
            return StageDecision("MYSQL_METADATA_DISCOVERY")
        if candidate_exists:
            return StageDecision("FLAG_VERIFICATION")
        return StageDecision("BOUNDED_EXTRACTION")


stage_decider = StageDecider()


def decide_required_stage(*, asset_warranty_mysql: bool, verified_fact_keys: set[str],
                          capability_ledger: dict | None = None, candidate_exists: bool = False,
                          declared_fields: set[str] | None = None, tested_fields: set[str] | None = None) -> StageDecision:
    return stage_decider.decide(
        asset_warranty_mysql=asset_warranty_mysql,
        verified_fact_keys=verified_fact_keys,
        capability_ledger=capability_ledger,
        candidate_exists=candidate_exists,
        declared_fields=declared_fields,
        tested_fields=tested_fields,
    )
