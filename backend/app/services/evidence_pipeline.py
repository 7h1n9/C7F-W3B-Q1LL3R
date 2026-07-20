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
    def normalize(self, tool: str, result: dict | None, artifact_ids: list[str] | None = None) -> ToolModelView:
        result = result or {}
        facts = result.get("facts") if isinstance(result.get("facts"), dict) else result
        signals = [str(x) for x in result.get("matched_signals", [])] if isinstance(result.get("matched_signals"), list) else []
        return ToolModelView(tool, result.get("status") in {None, "COMPLETED"} and not result.get("error_code"), facts, artifact_ids or [], signals)

    def apply(self, view: ToolModelView, ledger: dict, plan: dict) -> tuple[dict, dict, dict | None]:
        capabilities = set(view.signals)
        capabilities.update({"can_read_public_page"} if view.tool == "http_request" and view.ok else set())
        for capability in capabilities:
            ledger, plan, node = reduce_capability(ledger, plan, capability, {"tool": view.tool, "artifact_ids": view.artifact_ids})
        return ledger, plan, node
