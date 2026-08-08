from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..graph import MutekiGraph
from ..recon.breadth_scanner import BreadthScanner, ReconReport
from ..recon.fingerprint import ClassificationResult, classify_challenge


@dataclass(frozen=True, slots=True)
class RaceResult:
    classification: ClassificationResult
    report: ReconReport | None = None
    flag_found: bool = False


class RaceWorker:
    """Perform metadata fast-path or bounded breadth reconnaissance."""

    def __init__(self, graph: MutekiGraph, execute_tool, *, target_url: str, metadata: Mapping[str, Any] | None = None, workspace_id: str = "", run_id: str = "") -> None:
        self.graph = graph
        self.execute_tool = execute_tool
        self.target_url = target_url
        self.metadata = dict(metadata or {})
        self.workspace_id = workspace_id
        self.run_id = run_id

    async def run(self, worker_id: str = "race-worker") -> RaceResult:
        explicit = classify_challenge(self.metadata)
        if explicit and explicit.classification == "SQLI":
            self._fact(worker_id, {"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": explicit.confidence, "reason": explicit.reason})
            return RaceResult(explicit)

        report = await BreadthScanner(self.execute_tool).scan(
            base_url=self.target_url,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
        )
        for observation in report.observations:
            self._fact(worker_id, {
                "type": "ENDPOINT_OBSERVED",
                "endpoint": observation.endpoint,
                "status_code": observation.status_code,
                "summary": observation.summary,
                "auth_required": observation.status_code in {401, 403} or observation.redirected_to_login or any(marker in observation.summary.casefold() for marker in ("login", "sign in", "password")),
                "session_cookie_names": list(observation.cookie_names),
                "framework": observation.framework,
                "jwt": observation.jwt_detected,
                "evidence_refs": list(observation.evidence_refs),
            })
        discovered = set(report.endpoints)
        self._fact(worker_id, {"type": "ENDPOINTS_DISCOVERED", "endpoints": [{"endpoint": item.endpoint, "status_code": item.status_code, "auth_required": item.status_code in {401, 403} or item.redirected_to_login or any(marker in item.summary.casefold() for marker in ("login", "sign in", "password")), "jwt": item.jwt_detected} for item in report.observations if item.endpoint in discovered], "evidence_refs": list(report.evidence_refs)})
        self._fact(worker_id, {"type": "AUTH_REQUIRED", "value": report.auth_required, "evidence_refs": list(report.evidence_refs)})
        self._fact(worker_id, {"type": "SESSION_COOKIE", "names": list(report.session_cookie_names), "evidence_refs": list(report.evidence_refs)})
        self._fact(worker_id, {"type": "FRAMEWORK", "names": list(report.frameworks), "evidence_refs": list(report.evidence_refs)})
        classification = classify_challenge(self.metadata, self.graph.facts()) or ClassificationResult("GENERIC_WEB", 40, "no high-confidence signature", report.evidence_refs)
        self._fact(worker_id, {"type": "CHALLENGE_CLASSIFICATION", "classification": classification.classification, "confidence": classification.confidence, "reason": classification.reason, "evidence_refs": list(classification.evidence_refs)})
        return RaceResult(classification, report)

    def _fact(self, worker_id: str, value: dict[str, Any]) -> None:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True)
        self.graph.add_fact(actor=worker_id, content=content, verified=bool(value.get("type") == "CHALLENGE_CLASSIFICATION" and int(value.get("confidence") or 0) >= 70), evidence_refs=list(value.get("evidence_refs") or []), dedupe_key=f"race:{value.get('type')}:{value.get('endpoint') or value.get('classification') or ''}")


__all__ = ["RaceResult", "RaceWorker"]
