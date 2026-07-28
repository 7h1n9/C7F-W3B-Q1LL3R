"""Runtime tool catalog snapshots for Codex Attempts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import AttemptToolManifest, RunAttempt, SolveRun
from app.services.events import event_service
from app.services.runner_client import runner_client
from app.services.skill_selection import allowed_tools_for
from app.tools.registry import load_tool_definitions


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


async def refresh_runtime_tool_manifest(
    session: AsyncSession,
    run: SolveRun,
    attempt: RunAttempt,
    challenge: Challenge,
    *,
    mcp_tools: list[dict[str, Any]] | None = None,
) -> AttemptToolManifest:
    role = {str(item) for item in (run.role_snapshot_json or {}).get("tools", [])}
    challenge_tools = set(allowed_tools_for(challenge.challenge_type))
    definitions = load_tool_definitions()
    backend = {name for name, item in definitions.items() if item.enabled}
    try:
        capability = await runner_client.capabilities()
        rows = capability.get("tools") if isinstance(capability, dict) else []
        runner = {
            str(item.get("name"))
            for item in rows
            if isinstance(item, dict)
            and item.get("implemented", item.get("available", False))
            and item.get("installed", True)
            and item.get("enabled", True)
            and item.get("self_test_ok", True)
        }
    except Exception:
        runner = set()
    advertised_rows = [item for item in (mcp_tools or []) if isinstance(item, dict)]
    advertised = {str(item.get("name")) for item in advertised_rows if item.get("name")}
    schema_hashes = {
        str(item["name"]): _digest(item.get("inputSchema"))
        for item in advertised_rows
        if item.get("name")
    }
    effective = role & challenge_tools & backend & runner & advertised if advertised else set()
    expected = role & challenge_tools & backend
    missing = sorted(expected - effective)
    manifest_data = {
        "role_snapshot_tools": sorted(role),
        "challenge_allowed_tools": sorted(challenge_tools),
        "backend_registry_tools": sorted(backend),
        "runner_capability_tools": sorted(runner),
        "mcp_advertised_tools": sorted(advertised),
        "effective_tools": sorted(effective),
        "missing_expected_tools": missing,
        "schema_hashes": schema_hashes,
    }
    item = await session.scalar(select(AttemptToolManifest).where(AttemptToolManifest.attempt_id == attempt.id))
    if item is None:
        item = AttemptToolManifest(run_id=run.id, attempt_id=attempt.id, **manifest_data, manifest_sha256=_digest(manifest_data))
        session.add(item)
    else:
        for key, value in manifest_data.items():
            setattr(item, key, value)
        item.manifest_sha256 = _digest(manifest_data)
    if missing:
        await event_service.append(
            session,
            run.id,
            "attempt.tool_catalog_drift",
            {
                "code": "TOOL_CATALOG_DRIFT",
                "expected": sorted(expected),
                "runner_available": sorted(runner),
                "mcp_advertised": sorted(advertised),
                "effective": sorted(effective),
                "recommended_action": "restart_backend_runner_bridge",
            },
        )
    await session.flush()
    return item
