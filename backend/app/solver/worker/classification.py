from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..shared_graph import ChallengeClassification, ClassificationFact, SharedGraph


@dataclass(frozen=True, slots=True)
class ClassificationWorker:
    """Bounded, deterministic classifier that writes only a typed fact.

    The worker does not execute requests itself.  Its host supplies at most
    three observed HTTP results, and this component applies hard-coded
    response/parameter fingerprints before persisting the classification.
    """

    graph: SharedGraph
    timeout_seconds: int = 60
    max_http_requests: int = 3

    def classify(
        self,
        observations: Sequence[Mapping[str, Any]],
        *,
        source_worker_id: str,
    ) -> ClassificationFact:
        if len(observations) > self.max_http_requests:
            raise ValueError(f"classification worker accepts at most {self.max_http_requests} observations")
        classification, confidence, evidence_refs = self._infer(observations)
        fact_id = self.graph.write_classification(
            classification,
            confidence=confidence,
            evidence_refs=evidence_refs,
            source_worker_id=source_worker_id,
        )
        return ClassificationFact(classification, confidence, tuple(evidence_refs), fact_id, source_worker_id)

    @staticmethod
    def _infer(observations: Sequence[Mapping[str, Any]]) -> tuple[ChallengeClassification, int, list[str]]:
        text = "\n".join(str(item.get(key, "")) for item in observations for key in ("response", "body", "headers", "parameters", "request_body", "cookies"))
        lowered = text.casefold()
        refs = [str(item["evidence_ref"]) for item in observations if item.get("evidence_ref")]
        parameter_text = " ".join(str(item.get("parameters", "")) for item in observations).casefold()
        request_body = " ".join(str(item.get("request_body", "")) for item in observations).casefold()

        if any(marker in lowered for marker in ("sql", "mysql", "sqlite", "syntax error")):
            return ChallengeClassification.SQLI, 92, refs
        if any(name in parameter_text for name in ("file", "path", "dir", "read")) and any(marker in lowered for marker in ("root:", "[boot loader]", "<?xml", "file content")):
            return ChallengeClassification.PATH_TRAVERSAL, 88, refs
        if any(marker in lowered for marker in ("ping", "whoami", "command output")) and any(marker in lowered for marker in (";", "|", "&&")):
            return ChallengeClassification.COMMAND_INJECTION, 86, refs
        if any(marker in parameter_text for marker in ("http://", "https://")) and any(marker in lowered for marker in ("127.0.0.1", "169.254.169.254", "localhost", "internal")):
            return ChallengeClassification.SSRF, 86, refs
        if "jwt" in lowered or "jwt" in " ".join(str(item.get("cookies", "")) for item in observations).casefold():
            return ChallengeClassification.JWT, 84, refs
        if "{{7*7}}" in lowered and "49" in lowered:
            return ChallengeClassification.SSTI, 86, refs
        if "<!doctype" in request_body and "system" in request_body:
            return ChallengeClassification.XXE, 88, refs
        if "multipart/form-data" in lowered:
            return ChallengeClassification.FILE_UPLOAD, 86, refs
        return ChallengeClassification.GENERIC_WEB, 30, refs


__all__ = ["ClassificationWorker"]
