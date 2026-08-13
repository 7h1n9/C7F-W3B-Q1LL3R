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
    links = [str(link) for item in endpoints for link in (item.get("links") or []) if isinstance(link, str)]
    disclosed = [str(path) for item in endpoints for path in (item.get("disclosed_paths") or []) if isinstance(path, str)]
    all_paths = paths + links
    text = " ".join(
        str(item.get(key) or "")
        for item in endpoints
        for key in ("endpoint", "summary", "form_actions", "parameter_names", "framework")
    ).casefold()
    if any("file=" in path.casefold() for path in all_paths):
        return ClassificationResult("PATH_TRAVERSAL", 85, "file parameter discovered", tuple(dict.fromkeys(refs)))
    if any("/preview" in path.casefold() and "path=" in path.casefold() for path in all_paths) or disclosed:
        return ClassificationResult("PATH_TRAVERSAL", 88, "document preview path and archive reference discovered", tuple(dict.fromkeys(refs)))
    # A login form on the home page is not enough to call the challenge IDOR.
    # Require the candidate object/list endpoint itself to be protected (or to
    # expose a successful object/list response).  This prevents a 404 probe
    # such as ``/tickets`` from hijacking unrelated authenticated workflows.
    protected_object_surface = any(
        path.rstrip("/").endswith(("/tickets", "/dashboard"))
        and (bool(item.get("auth_required")) or int(item.get("status_code") or 0) in {200, 206})
        for item, path in ((item, str(item.get("endpoint") or item.get("url") or "")) for item in endpoints)
    )
    if protected_object_surface:
        return ClassificationResult("IDOR", 85, "object/list endpoint behind authentication", tuple(dict.fromkeys(refs)))
    if any("upload" in path.casefold() or "multipart" in path.casefold() for path in all_paths) or "multipart/form-data" in text:
        return ClassificationResult("FILE_UPLOAD", 82, "upload endpoint or multipart form discovered", tuple(dict.fromkeys(refs)))
    if any(item.get("jwt") for item in endpoints) or " jwt" in text or "jwt" in text:
        return ClassificationResult("JWT", 80, "JWT evidence discovered", tuple(dict.fromkeys(refs)))
    if any(marker in text for marker in ("cmd", "command", "exec", "ping")):
        return ClassificationResult("COMMAND_INJECTION", 78, "command-shaped parameter or response discovered", tuple(dict.fromkeys(refs)))
    if any(marker in text for marker in ("callback", "webhook", "fetch_url", "remote_url")):
        return ClassificationResult("SSRF", 78, "server-side URL fetch surface discovered", tuple(dict.fromkeys(refs)))
    if any(marker in text for marker in ("{{", "jinja", "template", "render")):
        return ClassificationResult("SSTI", 76, "template rendering surface discovered", tuple(dict.fromkeys(refs)))
    if "xml" in text or "doctype" in text:
        return ClassificationResult("XXE", 75, "XML processing surface discovered", tuple(dict.fromkeys(refs)))
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
