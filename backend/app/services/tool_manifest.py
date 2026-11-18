"""Runtime tool catalog snapshots for Codex Attempts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.run import AttemptToolManifest, RunAttempt, SolveRun
from app.services.events import event_service
from app.services.runner_client import runner_client
from app.services.runtime_build import backend_build_manifest
from app.services.skill_selection import allowed_tools_for
from app.tools.registry import load_tool_definitions

# These tools are useful accelerators, but they are not required to start a
# Codex Attempt.  Remote Runner builds may omit them; SQL extraction can use
# sql_boolean_compare plus the bounded script_run fallback instead.
OPTIONAL_MISSING_RUNTIME_TOOLS = {
    "binwalk_scan",
    "boolean_config_extract",
    "exiftool_metadata",
    "oracle_probe_matrix",
    "sqlmap_run",
    "sandbox_exec",
}


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
    challenge_tools = set(allowed_tools_for(challenge.challenge_type, challenge.metadata_json or {}))
    definitions = load_tool_definitions()
    backend = {name for name, item in definitions.items() if item.enabled}
    # The legacy Kali Runner gate.
    #
    # When it is part of the deployment, the manifest is intersected with the
    # capabilities that Runner advertises -- that intersection is the whole point
    # of the manifest, and it stays exactly as it was.
    #
    # When it is not (the container-native default), there is no Runner to ask and
    # asking anyway is actively harmful: an unreachable service yields an empty
    # capability set, ``effective`` becomes empty, every network tool is reported
    # as missing, and the Run is paused with ``TOOL_CATALOG_DRIFT`` -- a failure
    # whose message points at the tool catalog while the cause is a service the
    # deployment does not use.  So the intersection is skipped and the backend
    # registry is authoritative.
    legacy_runner = get_settings().legacy_kali_runner
    runner_health: dict = {}
    capability_payload: dict = {}
    if legacy_runner:
        try:
            runner_health = await runner_client.health()
        except Exception:
            runner_health = {}
        try:
            capability = await runner_client.capabilities()
            capability_payload = capability if isinstance(capability, dict) else {}
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
    else:
        runner = set(backend)
    # MCP is deliberately not part of the multi_agent_v1 controller loop.
    # Keep the argument for compatibility with the manual diagnostic path,
    # but never let an advertised catalog gate an Attempt.
    advertised_rows = []
    advertised = {str(item.get("name")) for item in advertised_rows if item.get("name")}
    schema_hashes = {
        str(item["name"]): _digest(item.get("inputSchema"))
        for item in advertised_rows
        if item.get("name")
    }
    effective = role & challenge_tools & backend & runner
    # Solver v2 may select a bounded DBMS fallback after observing that the
    # declared adapter produces no distinguishable metadata fact.  Keep the
    # legacy role/challenge intersection unchanged, but make the existing
    # SQLite metadata tool available to this explicit Solver path when the
    # Runner capability probe proves it is installed.
    if (
        getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2"
        and "sqlite_metadata_discovery" in backend
        and "sqlite_metadata_discovery" in runner
    ):
        effective.add("sqlite_metadata_discovery")
    if (
        getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2"
        and "script_run" in role
        and "script_run" in backend
        and "script_run" in runner
    ):
        effective.add("script_run")
    expected = role & challenge_tools & backend
    missing = sorted(expected - effective)
    runner_tool_rows = {str(item.get("name")): item for item in (capability_payload.get("tools") or []) if isinstance(item, dict)}
    network_enforcement = capability_payload.get("network_enforcement") if isinstance(capability_payload.get("network_enforcement"), dict) else {}
    script_row = runner_tool_rows.get("script_run") or {}
    # Allowlist enforcement is proven by the Runner's self-report, so it can only
    # be asserted when there is a Runner.  With the Runner retired the backend's
    # own Tool Gateway is the enforcement point, and treating "no Runner report"
    # as "unproven" would pause every networked Run (`SCRIPT_TARGET_NETWORK_UNAVAILABLE`)
    # for a service that is not in the path.
    network_contract_ok = (
        True
        if not legacy_runner
        else (
            "target_allowlist" in set(script_row.get("supported_network_modes") or [])
            and bool(script_row.get("target_allowlist_enforced"))
            and bool(network_enforcement.get("target_allowlist_enforced"))
        )
    )
    manifest_data = {
        "role_snapshot_tools": sorted(role),
        "challenge_allowed_tools": sorted(challenge_tools),
        "backend_registry_tools": sorted(backend),
        "runner_capability_tools": sorted(runner),
        "mcp_advertised_tools": [],
        "execution_mode": "controller_tool_loop",
        "mcp_required": False,
        "effective_tools": sorted(effective),
        "missing_expected_tools": missing,
        "schema_hashes": schema_hashes,
        "network_enforcement_json": network_enforcement,
        "tool_capabilities_json": {"script_run": {"supported_network_modes": script_row.get("supported_network_modes") or [], "target_allowlist_enforced": bool(network_contract_ok), "valid": network_contract_ok}},
    }
    attempt.runtime_build_manifest_json = {
        "backend": backend_build_manifest(),
        "runner": runner_health.get("build") if isinstance(runner_health, dict) else {},
        "bridge": {"mcp_enabled": False, "mcp_required": False},
    }
    item = await session.scalar(select(AttemptToolManifest).where(AttemptToolManifest.attempt_id == attempt.id))
    if item is None:
        item = AttemptToolManifest(run_id=run.id, attempt_id=attempt.id, **manifest_data, manifest_sha256=_digest(manifest_data))
        session.add(item)
    else:
        for key, value in manifest_data.items():
            setattr(item, key, value)
        item.manifest_sha256 = _digest(manifest_data)
    if "script_run" in expected and not network_contract_ok:
        missing.append("script_run.target_allowlist_enforced")
    blocking_missing = [item for item in missing if item not in OPTIONAL_MISSING_RUNTIME_TOOLS]
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
        if blocking_missing:
            run.status = "PAUSED_DEPLOYMENT"
            run.last_error_code = "SCRIPT_TARGET_NETWORK_UNAVAILABLE" if "script_run.target_allowlist_enforced" in missing else "TOOL_CATALOG_DRIFT"
            run.last_error_message = "Attempt tool manifest does not prove target allowlist enforcement." if run.last_error_code == "SCRIPT_TARGET_NETWORK_UNAVAILABLE" else "Attempt tool manifest does not match the advertised runtime catalog."
            attempt.tool_manifest_status = "DRIFT"
        elif missing:
            attempt.tool_manifest_status = "FALLBACK_READY"
    else:
        attempt.tool_manifest_status = "READY"
    await session.flush()
    return item
