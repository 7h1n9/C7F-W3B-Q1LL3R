"""Safe projection of facts produced by the official Muteki Worker.

The official Worker communicates with the Coordinator through the upstream
SharedGraph.  This module deliberately projects only a small, typed observation
contract from ``fact_added`` events.  It never forwards Worker output, HTTP
responses, credentials, cookies, tokens, or flags to the compatibility graph.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_ALLOWED_FIELDS = frozenset(
    {
        "test_field",
        "observation_status",
        "success",
        "boolean_oracle_confirmed",
        "oracle_verified",
        "extraction_verified",
        "verification_method",
        "request_count",
        "status_code",
        "endpoint",
        "dbms",
        "stage",
        "target_expression",
        "capabilities",
        "extraction_strategy",
        "database",
        "tables",
        "columns",
    }
)
_BOOLEAN_FIELDS = frozenset({"success", "boolean_oracle_confirmed", "oracle_verified", "extraction_verified"})
_INTEGER_FIELDS = frozenset({"request_count", "status_code"})
_OBSERVATION_STATUSES = frozenset({"UNAVAILABLE"})
_LIST_FIELDS = frozenset({"tables", "columns", "capabilities"})
_METADATA_TARGET_EXPRESSIONS = frozenset(
    {
        "DATABASE()",
        "information_schema.tables",
        "information_schema.columns",
    }
)
_METADATA_STAGES = frozenset({"database", "tables", "columns"})
_EXTRACTION_VERIFICATION_METHODS = frozenset(
    {"artifact_witness", "blackboard_flag_gate"}
)


@dataclass(frozen=True, slots=True)
class SafeNativeObservation:
    """A bounded, non-sensitive observation extracted from one graph fact."""

    fact_sequence: int
    action_name: str
    fields: tuple[tuple[str, Any], ...]
    source_evidence_refs: tuple[str, ...] = ()

    def payload(self, *, evidence_refs: Iterable[str] = ()) -> dict[str, Any]:
        """Return the only representation allowed to cross the adapter seam."""

        result: dict[str, Any] = {
            "tool": self.action_name,
            "source_fact_seq": self.fact_sequence,
        }
        result.update(dict(self.fields))
        refs = [str(value) for value in evidence_refs if str(value)]
        if refs:
            result["evidence_refs"] = refs[:16]
        return result


def parse_safe_observation(content: Any, *, fact_sequence: int = 0) -> dict[str, Any] | None:
    """Parse a JSON observation while dropping all fields outside the contract.

    Official ``write-fact`` stores the fact as text, so accepting only a JSON
    object is intentional: free-form Worker prose must not become strategy
    state by accident.
    """

    if not isinstance(content, str) or len(content) > 4000:
        return None
    try:
        value = json.loads(_strip_fact_envelope(content))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    nested = value.get("observation")
    if isinstance(nested, Mapping):
        value = nested
    raw_tool = value.get("tool") or value.get("action_name") or value.get("action")
    tool = str(raw_tool or "").strip()
    if not _TOOL_RE.fullmatch(tool):
        return None

    result: dict[str, Any] = {"tool": tool}
    for key in _ALLOWED_FIELDS:
        if key not in value:
            continue
        normalized = _normalize_field(key, value[key])
        if normalized is not None:
            result[key] = normalized
    if len(result) == 1:
        return None
    if fact_sequence > 0:
        result["source_fact_seq"] = int(fact_sequence)
    return result


def _strip_fact_envelope(content: str) -> str:
    """Remove only the official marker/engine envelope around a JSON fact.

    ``CliSolver`` stores a verified marker as ``[engine] <fact>`` in the
    SharedGraph.  The envelope is metadata, not an observation field.  This
    helper deliberately does not search arbitrary prose for JSON, so a model
    sentence cannot become Solver state accidentally.
    """

    value = content.strip()
    marker = re.match(r"^\[[A-Za-z0-9_.:-]{1,80}\]\s*", value)
    if marker:
        value = value[marker.end() :].strip()
    for prefix in ("VERIFIED_FACT=", "FACT_JSON="):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    return value


def extract_native_observations(
    events: Iterable[Mapping[str, Any]],
    *,
    intent_id: str = "",
    expected_action: str = "",
) -> list[SafeNativeObservation]:
    """Extract safe observations from official ``fact_added`` event rows."""

    observations: list[SafeNativeObservation] = []
    for event in events:
        if str(event.get("kind") or "") != "fact_added":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        # Official events carry this bit when the witness gate accepted the
        # marker.  Older test/facade rows may omit it; an explicit false must
        # never be promoted into the next Strategy turn.
        if "verified" in event and not _is_verified(event.get("verified")):
            continue
        if "verified" in payload and not _is_verified(payload.get("verified")):
            continue
        event_intent = str(payload.get("intent_id") or "")
        if intent_id and event_intent and event_intent != intent_id:
            continue
        sequence = _safe_int(event.get("seq"), minimum=1)
        if sequence is None:
            continue
        parsed = parse_safe_observation(payload.get("fact"), fact_sequence=sequence)
        if not parsed:
            continue
        if expected_action and str(parsed.get("tool") or "") != expected_action:
            continue
        refs: list[str] = []
        artifact_id = str(event.get("artifact_id") or "")
        if artifact_id:
            refs.append(artifact_id)
        observations.append(
            SafeNativeObservation(
                fact_sequence=sequence,
                action_name=str(parsed.pop("tool")),
                fields=tuple(sorted(parsed.items())),
                source_evidence_refs=tuple(refs),
            )
        )
    return observations


def _is_verified(value: Any) -> bool:
    """Accept SQLite's integer boolean representation without widening trust."""

    return value is True or (
        isinstance(value, int) and not isinstance(value, bool) and value == 1
    )


def _normalize_field(key: str, value: Any) -> Any:
    if key in _BOOLEAN_FIELDS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
            return value.strip().casefold() == "true"
        return None
    if key in _INTEGER_FIELDS:
        return _safe_int(value, minimum=0, maximum=10000)
    if key in _LIST_FIELDS:
        if not isinstance(value, (list, tuple)):
            return None
        items = [
            str(item).strip()[:80]
            for item in value[:64]
            if _IDENTIFIER_RE.fullmatch(str(item).strip()[:80])
        ]
        return items or None
    if key == "target_expression":
        text = str(value).strip()[:80]
        return text if text in _METADATA_TARGET_EXPRESSIONS else None
    if key == "verification_method":
        text = str(value).strip()[:40]
        return text if text in _EXTRACTION_VERIFICATION_METHODS else None
    if key == "stage":
        text = str(value).strip()[:32]
        return text if text in _METADATA_STAGES else None
    if key == "observation_status":
        text = str(value).strip().upper()[:32]
        return text if text in _OBSERVATION_STATUSES else None
    if key in {"test_field", "database", "dbms", "extraction_strategy"}:
        text = str(value).strip()[:80]
        return text if _IDENTIFIER_RE.fullmatch(text) else None
    if key == "endpoint":
        text = str(value).strip()[:240]
        if not text or "\r" in text or "\n" in text:
            return None
        parsed = urlsplit(text)
        path = parsed.path if parsed.scheme or parsed.netloc else text.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/") or len(path) > 200:
            return None
        return path
    return None


def _safe_int(value: Any, *, minimum: int, maximum: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result < minimum or (maximum is not None and result > maximum):
        return None
    return result


__all__ = [
    "SafeNativeObservation",
    "extract_native_observations",
    "parse_safe_observation",
]
