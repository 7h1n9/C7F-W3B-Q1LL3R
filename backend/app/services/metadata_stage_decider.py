"""Deterministic essential MySQL metadata stage progression."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataStageDecision:
    stage: str | None
    target_expression: str | None
    next_stage: str | None
    all_essential_blocked: bool = False


class MetadataStageDecider:
    ORDER = ("database", "tables", "columns")
    TARGETS = {
        "database": "DATABASE()",
        "tables": "information_schema.tables",
        "columns": "information_schema.columns",
    }

    def decide(self, progress: dict | None, verified_fact_keys: set[str] | None = None) -> MetadataStageDecision:
        progress = progress or {}
        facts = verified_fact_keys or set()
        fact_by_stage = {
            "database": "asset_warranty.current_database",
            "tables": "asset_warranty.mysql_user_tables",
            "columns": "asset_warranty.mysql_candidate_columns",
        }
        for index, stage in enumerate(self.ORDER):
            item = progress.get(stage) if isinstance(progress.get(stage), dict) else {}
            status = str(item.get("status") or "PENDING").upper()
            if fact_by_stage[stage] in facts or status == "SUCCESS":
                continue
            if status == "BLOCKED":
                continue
            next_stage = self.ORDER[index + 1] if index + 1 < len(self.ORDER) else None
            return MetadataStageDecision(stage, self.TARGETS[stage], next_stage)
        return MetadataStageDecision(None, None, None, all_essential_blocked=True)


metadata_stage_decider = MetadataStageDecider()
