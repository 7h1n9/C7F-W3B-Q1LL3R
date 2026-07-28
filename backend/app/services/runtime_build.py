"""Runtime build identity used to prevent mixed backend/Runner/Bridge runs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from app.tools.registry import load_tool_definitions


def _git_sha(root: Path) -> str:
    configured = os.getenv("GIT_SHA") or os.getenv("BUILD_SHA")
    if configured:
        return configured[:80]
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            return (root / ".git" / ref).read_text(encoding="utf-8").strip()[:80]
        return head[:80]
    except OSError:
        return "unknown"


def tool_registry_hash() -> str:
    payload = {
        name: {"enabled": definition.enabled, "parameters": definition.parameters, "limits": definition.limits, "permissions": definition.permissions}
        for name, definition in sorted(load_tool_definitions().items())
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def backend_build_manifest() -> dict:
    root = Path(__file__).resolve().parents[3]
    return {
        "component": "backend",
        "git_sha": _git_sha(root),
        "build_id": os.getenv("BACKEND_BUILD_ID") or os.getenv("BUILD_ID") or "dev",
        "tool_registry_hash": tool_registry_hash(),
        "mcp_schema_version": "mcp-v1",
    }


def compare_builds(expected: dict, actual: dict) -> list[str]:
    mismatches: list[str] = []
    for key in ("git_sha", "build_id", "tool_registry_hash", "mcp_schema_version"):
        left, right = expected.get(key), actual.get(key)
        if left not in (None, "", "unknown", "dev") and right not in (None, "", "unknown", "dev") and left != right:
            mismatches.append(key)
    return mismatches
