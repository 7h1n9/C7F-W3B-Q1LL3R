from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SQLI_MARKERS = frozenset({"SQLI", "SQL_INJECTION", "SQLINJECTION", "SQL_INJECTION_GOLDEN"})
SUPPORTED = frozenset({"SQLI", "PATH_TRAVERSAL", "COMMAND_INJECTION", "SSRF", "IDOR", "JWT", "SSTI", "XXE", "FILE_UPLOAD", "GENERIC_WEB"})

# A CVE id in the Challenge's own name/description is the strongest signal there
# is: the arena roster is one-CVE-per-box ("[Spring]CVE-2016-4977"), and the
# recipe for that CVE already knows its family.  Keyed by the trailing
# ``YYYY-NNNN`` so ``CVE-2018-12613``, ``CVE_2018_12613`` and a bare
# ``2018-12613`` all hit the same entry.
_CVE_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("2018-12613", "PATH_TRAVERSAL", "phpMyAdmin index.php target LFI"),
    ("2018-3760", "PATH_TRAVERSAL", "Rails Sprockets path traversal"),
    ("2016-4977", "SSTI", "Spring Security OAuth2 SpEL injection"),
    ("2018-2894", "FILE_UPLOAD", "WebLogic ws_utc arbitrary file write"),
    ("2017-12615", "FILE_UPLOAD", "Tomcat PUT arbitrary file write"),
    ("2018-2628", "COMMAND_INJECTION", "WebLogic T3 deserialization"),
    ("2017-10271", "COMMAND_INJECTION", "WebLogic WLS-XMLDecoder deserialization"),
    ("2021-44228", "COMMAND_INJECTION", "Log4Shell JNDI lookup"),
)

# Keys that carry a CVE reference or a human title.  ``adapter`` is deliberately
# absent: it names the *platform execution adapter* (``dasctf_arena``).
_CVE_TEXT_KEYS = ("name", "title", "description", "cve_id", "friendly_id", "tags", "category")


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
    # The CVE id comes before any inference: it is declarative, and getting it
    # wrong sends the whole round down the wrong tool domain.
    by_cve = _classify_by_cve(metadata)
    if by_cve is not None:
        return by_cve
    # ``adapter`` is the platform execution adapter (``"dasctf_arena"``), not a
    # vulnerability signal.  It used to be treated as proof of a SQL challenge
    # at confidence 100, which classified *every* arena box as SQLI -- that is
    # how a Spring SpEL box and a phpMyAdmin LFI box both got dispatched
    # ``sql_boolean_compare`` and burned their whole budget on a SQL oracle the
    # target never had.  Only a declared DBMS counts, and only as a hint.
    if _declares_sql_surface(metadata):
        return ClassificationResult("SQLI", 90, "declared dbms with SQL surface")

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


def _classify_by_cve(metadata: Mapping[str, Any]) -> ClassificationResult | None:
    """Map a declared CVE id to its vulnerability family.

    Separators are normalised (``CVE-2018-12613``, ``CVE_2018_12613`` and
    ``CVE 2018 12613`` all collapse to ``2018-12613``) because the arena titles
    are authored upstream and are not consistent.
    """

    text = " ".join(str(metadata.get(key) or "") for key in _CVE_TEXT_KEYS)
    if not text.strip():
        return None
    normalized = text.upper().replace("_", "-").replace(" ", "-")
    for cve, family, reason in _CVE_FAMILIES:
        if cve in normalized:
            return ClassificationResult(family, 95, f"CVE {cve}: {reason}")
    return None


def _declares_sql_surface(metadata: Mapping[str, Any]) -> bool:
    """True only when the metadata names *both* a DBMS and a SQL surface.

    A bare ``dbms`` field is not enough -- plenty of web boxes bundle MySQL
    without exposing an injectable parameter -- so the declaration has to name
    the injection surface as well.
    """

    dbms = str(metadata.get("dbms") or "").strip().lower()
    if not dbms:
        return False
    declared = " ".join(
        str(metadata.get(key) or "")
        for key in (
            "vulnerability_type",
            "attack_surface",
            "surface",
            "tags",
            "description",
        )
    ).lower()
    return any(marker in declared for marker in ("sql", "sqli", "injection", "query"))


def _normalize(value: Any) -> str | None:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw in SQLI_MARKERS:
        return "SQLI"
    if raw in SUPPORTED:
        return raw
    aliases = {"PATH_TRAVERSAL": "PATH_TRAVERSAL", "COMMANDINJECTION": "COMMAND_INJECTION", "GENERIC": "GENERIC_WEB"}
    return aliases.get(raw)


__all__ = ["ClassificationResult", "classify_challenge"]
