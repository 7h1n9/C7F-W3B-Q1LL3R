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
               declared_fields: set[str] | None = None, tested_fields: set[str] | None = None,
               metadata_progress: dict | None = None) -> StageDecision:
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
        metadata_order = [
            ("asset_warranty.mysql_version", "version", False),
            ("asset_warranty.mysql_version_comment", "version_comment", False),
            ("asset_warranty.current_database", "database", True),
            ("asset_warranty.mysql_user_tables", "tables", True),
            ("asset_warranty.mysql_candidate_columns", "columns", True),
        ]
        progress = metadata_progress or (capability_ledger or {}).get("metadata_progress") or {}
        blocked_essential = []
        optional_blocked = any(
            str((progress.get(stage) or {}).get("status") or "").upper() == "BLOCKED"
            for stage in ("version", "version_comment")
        )
        for fact_key, metadata_stage, essential in metadata_order:
            if fact_key not in verified_fact_keys:
                status = str((progress.get(metadata_stage) or {}).get("status") or "PENDING").upper()
                if not essential and optional_blocked:
                    continue
                if status == "BLOCKED":
                    if essential:
                        blocked_essential.append(metadata_stage)
                    continue
                return StageDecision("MYSQL_METADATA_DISCOVERY", details={"stage": metadata_stage, "missing_fact": fact_key, "metadata_status": status})
        if len(blocked_essential) == 3:
            return StageDecision("WAITING_USER", requires_user=True, reason="All essential MySQL metadata stages are blocked.", details={"blocked_stages": blocked_essential})
        if candidate_exists:
            return StageDecision("FLAG_VERIFICATION")
        return StageDecision("BOUNDED_EXTRACTION")


stage_decider = StageDecider()


def decide_required_stage(*, asset_warranty_mysql: bool, verified_fact_keys: set[str],
                          capability_ledger: dict | None = None, candidate_exists: bool = False,
                          declared_fields: set[str] | None = None, tested_fields: set[str] | None = None,
                          metadata_progress: dict | None = None) -> StageDecision:
    return stage_decider.decide(
        asset_warranty_mysql=asset_warranty_mysql,
        verified_fact_keys=verified_fact_keys,
        capability_ledger=capability_ledger,
        candidate_exists=candidate_exists,
        declared_fields=declared_fields,
        tested_fields=tested_fields,
        metadata_progress=metadata_progress,
    )


async def decide_required_stage_for_run(session, run, challenge) -> StageDecision:
    from sqlalchemy import select
    from app.models.multi_agent import VerifiedFact
    from app.models.run import FlagCandidate, ToolCall
    from app.models.solver_state import SolverState

    keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
        VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"
    ))).all())
    metadata = (challenge.metadata_json or {}) if challenge else {}
    asset_mysql = str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql"
    candidate = bool(await session.scalar(select(FlagCandidate.id).where(
        FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(False),
        FlagCandidate.source_artifact_id.is_not(None), FlagCandidate.source_tool_call_id.is_not(None),
    )))
    calls = list((await session.scalars(select(ToolCall).where(
        ToolCall.run_id == run.id, ToolCall.tool_name == "sql_boolean_compare", ToolCall.status == "COMPLETED"
    ))).all())
    tested = {str((call.arguments_json or {}).get("test_field")) for call in calls if isinstance(call.arguments_json, dict) and (call.arguments_json or {}).get("test_field")}
    state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
    ledger = dict(state.capability_ledger_json or {}) if state else {}
    return stage_decider.decide(
        asset_warranty_mysql=asset_mysql, verified_fact_keys=keys, candidate_exists=candidate,
        declared_fields={str(item) for item in (metadata.get("fields") or []) if str(item)}, tested_fields=tested,
        capability_ledger=ledger, metadata_progress=ledger.get("metadata_progress") or {},
    )
