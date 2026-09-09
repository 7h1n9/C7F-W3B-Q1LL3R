"""Blackboard context summarizer — compress the growing fact/dead-end/intent log
into a bounded summary for the Worker context window.

The official Muteki ``solver/summarizer.py`` compresses stale facts when the
blackboard grows beyond a threshold, keeping the Worker prompt from becoming
unbounded.  This module mirrors that pattern.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# Maximum number of facts to include in the active summary before compression
_MAX_FACTS = 48
# Maximum number of dead ends to include
_MAX_DEAD_ENDS = 24
# Maximum number of intents to include
_MAX_INTENTS = 16
# Maximum number of evidence refs to keep per fact
_MAX_EVIDENCE_REFS = 4


def compress_facts(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compress a list of blackboard facts into a bounded summary.

    Keeps verified facts first (most recent), then candidate facts, up to
    ``_MAX_FACTS`` total.  Facts beyond the limit are aggregated into a
    count summary.
    """
    verified = [f for f in facts if _is_verified(f)]
    candidates = [f for f in facts if not _is_verified(f)]

    # Sort verified by sequence descending, then candidates
    verified.sort(key=_sort_key, reverse=True)
    candidates.sort(key=_sort_key, reverse=True)

    result: list[dict[str, Any]] = []
    for f in (verified + candidates):
        if len(result) >= _MAX_FACTS:
            break
        entry = {
            "seq": _int(f, "sequence", 0),
            "content": _truncate(str(f.get("content") or ""), 200),
            "verified": _is_verified(f),
        }
        refs = _list(f, "evidence_refs")
        if refs:
            entry["evidence_refs"] = refs[:_MAX_EVIDENCE_REFS]
        route = str(f.get("route_hash") or "")
        if route:
            entry["route"] = route[:20]
        result.append(entry)

    total = len(facts)
    if total > len(result):
        result.append({
            "seq": 0,
            "content": f"... and {total - len(result)} more facts "
                       f"({len(verified)} verified, {len(candidates)} candidates, "
                       f"{total} total).  Use the blackboard skill to read the full list.",
            "verified": False,
        })

    return result


def compress_dead_ends(dead_ends: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Compress dead ends into a bounded summary."""
    items = (list(dead_ends) or [])[:_MAX_DEAD_ENDS]
    result: list[dict[str, str]] = []
    for de in items:
        result.append({
            "description": _truncate(str(de.get("description") or de.get("content") or ""), 150),
        })
    total = len(dead_ends or [])
    if total > len(result):
        result.append({"description": f"... and {total - len(result)} more dead ends"})
    return result


def compress_intents(intents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compress intents into a bounded summary."""
    items = (list(intents) or [])[:_MAX_INTENTS]
    result: list[dict[str, Any]] = []
    for intent in items:
        entry: dict[str, Any] = {
            "id": str(intent.get("id") or intent.get("intent_id") or "")[:20],
            "description": _truncate(str(intent.get("description") or ""), 120),
            "status": str(intent.get("status") or "open"),
        }
        worker = str(intent.get("claimed_by") or "")
        if worker:
            entry["worker"] = worker[:30]
        result.append(entry)
    total = len(intents or [])
    if total > len(result):
        result.append({"description": f"... and {total - len(result)} more intents"})
    return result


def build_compressed_context(
    blackboard: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a compressed view of the full blackboard for the Worker context.

    ``blackboard`` should be a dict with keys ``facts``, ``dead_ends``, ``intents``,
    and optionally ``classification``, ``revision``.
    """
    return {
        "revision": blackboard.get("revision", 0),
        "classification": str(blackboard.get("classification") or ""),
        "facts": compress_facts(blackboard.get("facts") or []),
        "dead_ends": compress_dead_ends(blackboard.get("dead_ends") or []),
        "intents": compress_intents(blackboard.get("intents") or []),
    }


# ── helpers ────────────────────────────────────────────────────────────────


def _is_verified(fact: Mapping[str, Any]) -> bool:
    return bool(fact.get("verified"))


def _int(obj: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(obj.get(key) or default)
    except (TypeError, ValueError):
        return default


def _list(obj: Mapping[str, Any], key: str) -> list[str]:
    raw = obj.get(key)
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    return []


def _sort_key(fact: Mapping[str, Any]) -> int:
    return _int(fact, "sequence", 0) or _int(fact, "seq", 0)


def _truncate(text: str, max_len: int) -> str:
    return text[:max_len] if len(text) > max_len else text