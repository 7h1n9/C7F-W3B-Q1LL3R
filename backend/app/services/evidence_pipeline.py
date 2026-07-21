"""Normalize Runner/Codex output into one model-facing evidence contract."""
from dataclasses import dataclass
from typing import Any

from .attack_chain import reduce_capability


@dataclass
class ToolModelView:
    tool: str
    ok: bool
    facts: dict[str, Any]
    artifact_ids: list[str]
    signals: list[str]

class EvidencePipeline:
    CAPABILITIES = {
        "baseline_confirmed", "entry_identified", "session_established",
        "object_reference_discovered", "authorization_boundary_tested", "idor_confirmed",
        "sql_syntax_signal", "sql_injection_confirmed", "union_injection_confirmed", "has_flag_candidate",
    }

    def infer_capabilities(self, view: ToolModelView) -> set[str]:
        facts = view.facts if isinstance(view.facts, dict) else {}
        structured = facts.get("structured_result") if isinstance(facts.get("structured_result"), dict) else facts
        capabilities = set(view.signals) & self.CAPABILITIES
        if view.ok and view.tool in {"http_request", "http_session_request"}:
            capabilities.add("baseline_confirmed")
        if view.ok and (structured.get("final_url") or structured.get("forms") or structured.get("links")):
            capabilities.add("entry_identified")
        if view.tool == "http_session_request" and view.ok:
            capabilities.add("session_established")
        if structured.get("sql_syntax_signal"):
            capabilities.add("sql_syntax_signal")
        if structured.get("sql_injection_confirmed"):
            capabilities.add("sql_injection_confirmed")
        if structured.get("union_confirmed"):
            capabilities.add("union_injection_confirmed")
        if structured.get("suspected_flags") or facts.get("flag_candidate_count"):
            capabilities.add("has_flag_candidate")
        return capabilities

    def normalize(self, tool: str, result: dict | None, artifact_ids: list[str] | None = None) -> ToolModelView:
        result = result or {}
        model_view = result.get("model_view") if isinstance(result.get("model_view"), dict) else {}
        facts = model_view.get("extracted_facts") if isinstance(model_view.get("extracted_facts"), dict) else result.get("facts") if isinstance(result.get("facts"), dict) else result
        if isinstance(result.get("structured_result"), dict):
            facts = {**facts, **result["structured_result"]}
        signals = [str(x) for x in result.get("matched_signals", [])] if isinstance(result.get("matched_signals"), list) else []
        return ToolModelView(tool, result.get("status") in {None, "COMPLETED"} and not result.get("error_code"), facts, artifact_ids or [], signals)

    def apply(self, view: ToolModelView, ledger: dict, plan: dict) -> tuple[dict, dict, dict | None]:
        capabilities = self.infer_capabilities(view)
        capabilities.update({"can_read_public_page"} if view.tool == "http_request" and view.ok else set())
        for capability in capabilities:
            ledger, plan, node = reduce_capability(ledger, plan, capability, {"tool": view.tool, "artifact_ids": view.artifact_ids})
        return ledger, plan, node


evidence_pipeline = EvidencePipeline()
