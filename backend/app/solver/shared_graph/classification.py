from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ChallengeClassification(StrEnum):
    SQLI = "SQLI"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    COMMAND_INJECTION = "COMMAND_INJECTION"
    SSRF = "SSRF"
    IDOR = "IDOR"
    JWT = "JWT"
    SSTI = "SSTI"
    XXE = "XXE"
    FILE_UPLOAD = "FILE_UPLOAD"
    GENERIC_WEB = "GENERIC_WEB"


@dataclass(frozen=True, slots=True)
class ClassificationFact:
    classification: ChallengeClassification
    confidence: int
    evidence_refs: tuple[str, ...]
    fact_id: str | None = None
    source_worker_id: str = ""

    @property
    def ready(self) -> bool:
        return self.confidence >= 70


__all__ = ["ChallengeClassification", "ClassificationFact"]
