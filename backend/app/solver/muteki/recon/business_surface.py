"""Project bounded reconnaissance into typed, safe business-surface facts.

This module is deliberately a projection layer.  It does not classify a
vulnerability and it does not choose an exploit action.  It only converts
observed navigation metadata into compact Blackboard facts that later
classification and strategy stages can consume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from .breadth_scanner import ReconObservation

BUSINESS_FACT_TYPES = (
    "ENDPOINT_CATALOG",
    "FORM_SURFACE",
    "OBJECT_REFERENCE",
    "AUTH_BOUNDARY",
    "WORKFLOW_STATE",
    "TEMPLATE_SURFACE",
    "DOCUMENT_PARSER_SURFACE",
)

_OBJECT_KEYS = frozenset(
    {
        "id",
        "item",
        "item_id",
        "ticket",
        "ticket_id",
        "ticket_no",
        "order",
        "order_id",
        "order_no",
        "project",
        "project_id",
        "contract",
        "contract_id",
        "document",
        "document_id",
        "request",
        "request_id",
        "approval",
        "approval_id",
    }
)
_WORKFLOW_KEYS = frozenset(
    {
        "state",
        "status",
        "stage",
        "decision",
        "approval",
        "approved",
        "review",
        "reviewer",
        "owner",
        "role",
        "department",
    }
)
_TEMPLATE_HINTS = frozenset(
    {
        "template",
        "preview",
        "render",
        "subject",
        "body",
        "content",
        "markup",
    }
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$")
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_SENSITIVE_QUERY_KEYS = frozenset({"authorization", "cookie", "jwt", "password", "secret", "session", "token"})


@dataclass(frozen=True, slots=True)
class BusinessSurfaceFact:
    """A typed Blackboard projection containing only safe metadata."""

    fact_type: str
    value: dict[str, object]
    evidence_refs: tuple[str, ...] = ()
    verified: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.fact_type,
            **self.value,
            "evidence_refs": list(self.evidence_refs),
        }


def derive_business_surface_facts(observations: Iterable[ReconObservation]) -> tuple[BusinessSurfaceFact, ...]:
    """Build bounded business-surface facts from recon observations.

    Only endpoint paths, field names, status/header-derived booleans, and
    allow-listed object references are retained.  Response bodies, cookies,
    tokens, and arbitrary query values are never copied into the facts.
    """

    items = tuple(observations)
    refs = tuple(dict.fromkeys(ref for item in items for ref in item.evidence_refs))
    facts: list[BusinessSurfaceFact] = []

    catalog = [_endpoint_entry(item) for item in items]
    if catalog:
        facts.append(BusinessSurfaceFact("ENDPOINT_CATALOG", {"entries": catalog}, refs, verified=bool(refs)))

    forms = [_form_entry(item) for item in items if item.form_actions or item.parameter_names]
    if forms:
        facts.append(BusinessSurfaceFact("FORM_SURFACE", {"forms": forms}, _refs_for(items, forms), verified=bool(refs)))

    objects = [_object_entry(item) for item in items]
    objects = [item for item in objects if item["references"]]
    if objects:
        facts.append(BusinessSurfaceFact("OBJECT_REFERENCE", {"surfaces": objects}, _refs_for(items, objects), verified=bool(refs)))

    auth = [_auth_entry(item) for item in items if _requires_auth(item)]
    if auth:
        facts.append(BusinessSurfaceFact("AUTH_BOUNDARY", {"boundaries": auth}, _refs_for(items, auth), verified=bool(refs)))

    workflow = [_workflow_entry(item) for item in items if _workflow_keys(item)]
    if workflow:
        facts.append(BusinessSurfaceFact("WORKFLOW_STATE", {"surfaces": workflow}, _refs_for(items, workflow), verified=bool(refs)))

    templates = [_template_entry(item) for item in items if _template_hints(item)]
    if templates:
        facts.append(BusinessSurfaceFact("TEMPLATE_SURFACE", {"surfaces": templates}, _refs_for(items, templates), verified=bool(refs)))

    documents = [_document_entry(item) for item in items if _document_signals(item)]
    if documents:
        facts.append(BusinessSurfaceFact("DOCUMENT_PARSER_SURFACE", {"surfaces": documents}, _refs_for(items, documents), verified=bool(refs)))

    return tuple(facts)


def _endpoint_entry(item: ReconObservation) -> dict[str, object]:
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "status_code": item.status_code,
        "content_type": _safe_text(item.content_type),
        "parameter_names": _safe_names(item.parameter_names),
        "auth_required": _requires_auth(item),
        "redirected_to_login": bool(item.redirected_to_login),
        "framework": _safe_text(item.framework),
        "json_keys": _safe_names(item.json_keys),
        "evidence_refs": list(item.evidence_refs),
    }


def _form_entry(item: ReconObservation) -> dict[str, object]:
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "actions": [_safe_endpoint(value) for value in item.form_actions],
        "methods": list(item.form_methods),
        "enctypes": list(item.form_enctypes),
        "parameter_names": _safe_names(item.parameter_names),
        "multipart": bool(item.multipart_detected),
    }


def _object_entry(item: ReconObservation) -> dict[str, object]:
    references: list[dict[str, str]] = []
    for link in item.links:
        parsed = urlparse(link)
        path = parsed.path or "/"
        segments = [segment for segment in path.split("/") if segment]
        for index, segment in enumerate(segments[:-1]):
            if segment.casefold() in {"ticket", "tickets", "order", "orders", "project", "projects", "contract", "contracts", "document", "documents", "request", "requests", "approval", "approvals", "item", "items"}:
                value = segments[index + 1]
                if _PATH_TOKEN.fullmatch(value):
                    references.append({"kind": segment.casefold().rstrip("s"), "value": value})
        for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
            if key.casefold() in _OBJECT_KEYS:
                for value in values[:3]:
                    if _SAFE_VALUE.fullmatch(value):
                        references.append({"kind": key.casefold(), "value": value})
    for key in _safe_names(item.parameter_names):
        if key.casefold() in _OBJECT_KEYS:
            references.append({"kind": key.casefold(), "value": "parameter"})
    return {"endpoint": _safe_endpoint(item.endpoint), "references": _unique_dicts(references), "evidence_refs": list(item.evidence_refs)}


def _auth_entry(item: ReconObservation) -> dict[str, object]:
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "status_code": item.status_code,
        "redirected_to_login": bool(item.redirected_to_login),
        "cookie_present": bool(item.cookie_names),
        "auth_required": _requires_auth(item),
        "evidence_refs": list(item.evidence_refs),
    }


def _workflow_entry(item: ReconObservation) -> dict[str, object]:
    keys = _workflow_keys(item)
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "state_keys": keys,
        "json_keys": [key for key in _safe_names(item.json_keys) if key.casefold() in _WORKFLOW_KEYS],
        "evidence_refs": list(item.evidence_refs),
    }


def _template_entry(item: ReconObservation) -> dict[str, object]:
    hints = _template_hints(item)
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "hints": hints,
        "parameter_names": _safe_names(item.parameter_names),
        "form_actions": [_safe_endpoint(value) for value in item.form_actions],
        "evidence_refs": list(item.evidence_refs),
    }


def _document_entry(item: ReconObservation) -> dict[str, object]:
    signals: list[str] = []
    if item.xml_detected:
        signals.append("xml")
    if item.multipart_detected:
        signals.append("multipart")
    text = f"{item.endpoint} {' '.join(item.parameter_names)}".casefold()
    if any(marker in text for marker in ("download", "preview", "file", "path", "document", "attachment")):
        signals.append("document_route")
    if item.disclosed_paths:
        signals.append("disclosed_path_reference")
    return {
        "endpoint": _safe_endpoint(item.endpoint),
        "content_type": _safe_text(item.content_type),
        "signals": list(dict.fromkeys(signals)),
        "parameter_names": _safe_names(item.parameter_names),
        "evidence_refs": list(item.evidence_refs),
    }


def _requires_auth(item: ReconObservation) -> bool:
    return bool(item.status_code in {401, 403} or item.redirected_to_login or item.cookie_names or "login" in item.endpoint.casefold())


def _workflow_keys(item: ReconObservation) -> list[str]:
    values = list(item.parameter_names) + list(item.json_keys)
    return list(dict.fromkeys(value for value in _safe_names(values) if value.casefold() in _WORKFLOW_KEYS))


def _template_hints(item: ReconObservation) -> list[str]:
    values = list(item.parameter_names) + list(item.json_keys) + [item.endpoint, *item.form_actions]
    tokens = [token for value in values for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{0,79}", str(value))]
    canonical = {"templates": "template", "previews": "preview", "renders": "render"}
    return list(dict.fromkeys(canonical.get(value.casefold(), value.casefold()) for value in tokens if any(hint in value.casefold() for hint in _TEMPLATE_HINTS)))


def _document_signals(item: ReconObservation) -> bool:
    text = f"{item.endpoint} {' '.join(item.parameter_names)}".casefold()
    return bool(item.xml_detected or item.multipart_detected or item.disclosed_paths or any(marker in text for marker in ("download", "preview", "file", "path", "document", "attachment")))


def _refs_for(items: tuple[ReconObservation, ...], entries: list[dict[str, object]]) -> tuple[str, ...]:
    endpoints = {str(entry.get("endpoint")) for entry in entries}
    return tuple(dict.fromkeys(ref for item in items if _safe_endpoint(item.endpoint) in endpoints for ref in item.evidence_refs))


def _safe_endpoint(value: str) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme and parsed.netloc:
        query_keys = sorted(key for key in parse_qs(parsed.query, keep_blank_values=False) if key.casefold() not in _SENSITIVE_QUERY_KEYS)
        suffix = "?" + "&".join(f"{key}=" for key in query_keys) if query_keys else ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}{suffix}"[:500]
    return str(value).split("?", 1)[0][:500]


def _safe_names(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", text) and text.casefold() not in {"authorization", "cookie", "token", "secret", "password"}:
            result.append(text)
    return list(dict.fromkeys(result))


def _safe_text(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:120]


def _unique_dicts(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for value in values:
        key = tuple(sorted(value.items()))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result[:30]


__all__ = ["BUSINESS_FACT_TYPES", "BusinessSurfaceFact", "derive_business_surface_facts"]
