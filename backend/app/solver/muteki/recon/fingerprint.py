from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SQLI_MARKERS = frozenset({"SQLI", "SQL_INJECTION", "SQLINJECTION", "SQL_INJECTION_GOLDEN"})
SUPPORTED = frozenset({"SQLI", "PATH_TRAVERSAL", "COMMAND_INJECTION", "SSRF", "IDOR", "JWT", "SSTI", "XXE", "FILE_UPLOAD", "GENERIC_WEB"})


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: str
    confidence: int
    reason: str
    evidence_refs: tuple[str, ...] = ()


def classify_challenge(metadata: Mapping[str, Any] | None, facts: Iterable[Any] = ()) -> ClassificationResult | None:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    explicit = _normalize(metadata.get("vulnerability_type"))
    if explicit:
        return ClassificationResult(explicit, 100, "explicit vulnerability_type metadata")
    if metadata.get("adapter") or metadata.get("dbms"):
        return ClassificationResult("SQLI", 100, "adapter/dbms metadata marks SQL challenge")

    endpoints: list[dict[str, Any]] = []
    refs: list[str] = []
    for fact in facts:
        content = fact.get("content", fact) if isinstance(fact, Mapping) else getattr(fact, "content", fact)
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            if value.get("type") == "ENDPOINT_OBSERVED":
                endpoints.append(value)
            if value.get("type") == "ENDPOINTS_DISCOVERED":
                endpoints.extend(item for item in value.get("endpoints", []) if isinstance(item, dict))
            refs.extend(str(item) for item in value.get("evidence_refs", []) or [])
            if value.get("classification"):
                normalized = _normalize(value.get("classification"))
                if normalized:
                    return ClassificationResult(normalized, int(value.get("confidence") or 70), "classification fact", tuple(dict.fromkeys(refs)))
    paths = [str(item.get("endpoint") or item.get("url") or "") for item in endpoints]
    if any("file=" in path.casefold() for path in paths):
        return ClassificationResult("PATH_TRAVERSAL", 85, "file parameter discovered", tuple(dict.fromkeys(refs)))
    if any(path.rstrip("/").endswith(("/tickets", "/dashboard")) for path in paths) and any(item.get("auth_required") for item in endpoints):
        return ClassificationResult("IDOR", 85, "object/list endpoint behind authentication", tuple(dict.fromkeys(refs)))
    if any("/api" in path.casefold() for path in paths) and any(item.get("jwt") for item in endpoints):
        return ClassificationResult("JWT", 80, "API and JWT evidence discovered", tuple(dict.fromkeys(refs)))
    if endpoints:
        return ClassificationResult("GENERIC_WEB", 55, "web surface discovered without a high-confidence signature", tuple(dict.fromkeys(refs)))
    return None


def _normalize(value: Any) -> str | None:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw in SQLI_MARKERS:
        return "SQLI"
    if raw in SUPPORTED:
        return raw
    aliases = {"PATH_TRAVERSAL": "PATH_TRAVERSAL", "COMMANDINJECTION": "COMMAND_INJECTION", "GENERIC": "GENERIC_WEB"}
    return aliases.get(raw)


__all__ = ["ClassificationResult", "classify_challenge"]
