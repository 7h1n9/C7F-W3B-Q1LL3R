from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.run import RunAttempt, RunExecutionLease, SolveRun, ToolInvocationTicket
from app.tools.registry import load_tool_definitions

SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
WORKSPACE_TOOLS = {
    "workspace_list", "workspace_tree", "workspace_stat", "workspace_read", "workspace_search",
    "workspace_write_file", "workspace_write_note", "workspace_patch_file", "workspace_mkdir",
    "workspace_copy", "workspace_move_generated", "workspace_delete_generated", "workspace_extract_archive",
}


def _schema_from_parameters(parameters: Any, name: str) -> dict:
    if not isinstance(parameters, dict) or not parameters:
        return {"type": "object", "additionalProperties": True}
    if parameters.get("type") == "object" and ("properties" in parameters or "additionalProperties" in parameters):
        return parameters
    properties: dict[str, dict] = {}
    required: list[str] = []
    for key, raw in parameters.items():
        spec = raw if isinstance(raw, dict) else {"type": raw}
        schema = {item: value for item, value in spec.items() if item != "required"}
        properties[key] = schema
        if isinstance(raw, dict) and raw.get("required") is True:
            required.append(key)
    return {"type": "object", "properties": properties, **({"required": required} if required else {}), "additionalProperties": False}


def validate_mcp_input_schema(value: Any, path: str = "$", depth: int = 0) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    if depth > 12:
        return [f"{path} exceeds nesting limit"]
    allowed = {"$ref", "$defs", "anyOf", "oneOf", "allOf", "not", "type", "enum", "const", "title", "description", "default", "examples", "format", "items", "properties", "required", "additionalProperties", "minimum", "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems"}
    errors = [f"{path}.{key} is not a JSON Schema keyword" for key in value if key not in allowed]
    if "type" in value and (not isinstance(value["type"], str) or value["type"] not in SCHEMA_TYPES):
        errors.append(f"{path}.type is invalid")
    if "required" in value and (not isinstance(value["required"], list) or any(not isinstance(item, str) for item in value["required"])):
        errors.append(f"{path}.required must be a string array")
    if "properties" in value:
        if not isinstance(value["properties"], dict):
            errors.append(f"{path}.properties must be an object")
        else:
            for key, child in value["properties"].items():
                errors.extend(validate_mcp_input_schema(child, f"{path}.properties.{key}", depth + 1))
    if value.get("type") == "array" and "items" not in value:
        errors.append(f"{path}.items is required for arrays")
    if "items" in value:
        errors.extend(validate_mcp_input_schema(value["items"], f"{path}.items", depth + 1))
    if "additionalProperties" in value and not isinstance(value["additionalProperties"], (bool, dict)):
        errors.append(f"{path}.additionalProperties is invalid")
    if isinstance(value.get("additionalProperties"), dict):
        errors.extend(validate_mcp_input_schema(value["additionalProperties"], f"{path}.additionalProperties", depth + 1))
    if "enum" in value and not isinstance(value["enum"], list):
        errors.append(f"{path}.enum must be an array")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in value and (not isinstance(value[key], (int, float)) or isinstance(value[key], bool)):
            errors.append(f"{path}.{key} must be numeric")
    return errors


class CodexPreflightService:
    def __init__(self) -> None:
        self._ready_runs: set[str] = set()
        self._last_result: dict[str, Any] | None = None

    def is_ready(self, run_id: str | None = None) -> bool:
        return bool(self._last_result and self._last_result.get("ready") and (not self._ready_runs or run_id in self._ready_runs))

    def last_result(self) -> dict[str, Any] | None:
        return self._last_result

    async def run(self, session: AsyncSession, run_id: str | None = None) -> dict[str, Any]:
        settings = get_settings()
        diagnostic_id = str(uuid.uuid4())
        stages: list[dict[str, Any]] = []
        temp_root: Path | None = None
        challenge = run = attempt = lease = ticket = None
        try:
            bridge = await self._bridge_health(settings.codex_bridge_url)
            stages.append({"stage": "BRIDGE_HEALTH", "ok": True, "details": bridge})
            sdk_version = self._sdk_version()
            stages.append({"stage": "SDK_VERSION", "ok": bool(sdk_version), "details": {"sdk_version": sdk_version}})
            cli_version = self._cli_version()
            stages.append({"stage": "CLI_EXECUTABLE", "ok": bool(cli_version), "details": {"cli_version": cli_version}})
            if not sdk_version or not cli_version:
                raise PreflightFailure("CLI_EXECUTABLE", "CODEX_CLI_NOT_FOUND", "Codex SDK or CLI is unavailable.")

            definitions = load_tool_definitions()
            rejected = []
            valid = []
            for name, definition in definitions.items():
                schema = _schema_from_parameters(definition.parameters, name)
                errors = validate_mcp_input_schema(schema)
                if errors:
                    rejected.append({"name": name, "errors": errors})
                elif definition.enabled:
                    valid.append(name)
            valid.extend(sorted(WORKSPACE_TOOLS))
            stages.append({"stage": "MCP_SCHEMA_VALIDATION", "ok": bool(valid), "details": {"valid_tools": valid, "rejected": rejected}})
            required_tools = {"http_request"} | WORKSPACE_TOOLS
            if not required_tools.issubset(set(valid)):
                raise PreflightFailure("MCP_SCHEMA_VALIDATION", "MCP_SCHEMA_INVALID", "Required ctfctl schemas are invalid.")

            temp_root = Path(tempfile.mkdtemp(prefix="codex-preflight-"))
            (temp_root / "challenge.json").write_text(json.dumps({"name": "preflight", "target_url": "http://preflight.invalid"}), encoding="utf-8")
            (temp_root / "source").mkdir()

            challenge = Challenge(name="Codex Preflight", target_url="http://preflight.invalid", allowed_hosts=["preflight.invalid"])
            session.add(challenge)
            await session.flush()
            run = SolveRun(challenge_id=challenge.id, workspace_path=str(temp_root), status="EXECUTING", current_phase="EXECUTING", engine_type="codex_sdk")
            session.add(run)
            await session.flush()
            attempt = RunAttempt(run_id=run.id, attempt_number=1, engine_type="codex_sdk", status="RUNNING")
            session.add(attempt)
            await session.flush()
            lease = RunExecutionLease(run_id=run.id, attempt_id=attempt.id, owner_instance_id="codex-preflight", lease_token=uuid.uuid4().hex, acquired_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=2))
            session.add(lease)
            await session.commit()
            mcp_stages, mcp_tools = await self._mcp_stdio_handshake(settings, temp_root, challenge, run, attempt, lease)
            stages.extend(mcp_stages)
            advertised_names = {str(item.get("name")) for item in mcp_tools if isinstance(item, dict)}
            if not required_tools.issubset(advertised_names):
                raise PreflightFailure("MCP_TOOL_CATALOG", "MCP_TOOL_CATALOG_FAILED", "The live MCP catalog is missing required tools.")
            schema_errors = []
            for item in mcp_tools:
                if isinstance(item, dict):
                    schema_errors.extend(validate_mcp_input_schema(item.get("inputSchema"), f"tools.{item.get('name')}"))
            if schema_errors:
                raise PreflightFailure("MCP_SCHEMA_VALIDATION", "MCP_SCHEMA_INVALID", "; ".join(schema_errors[:8]))
            # The MCP handshake mints one-shot tickets for list/read calls.
            # Remove those temporary rows before the explicit ticket test and
            # before deleting the temporary run in the finally block.
            await session.execute(
                delete(ToolInvocationTicket)
                .where(ToolInvocationTicket.run_id == run.id)
                .execution_options(synchronize_session=False)
            )
            await session.flush()
            await session.commit()
            raw_ticket = uuid.uuid4().hex
            ticket = ToolInvocationTicket(ticket_hash=hashlib.sha256(raw_ticket.encode()).hexdigest(), run_id=run.id, attempt_id=attempt.id, lease_id=lease.id, expires_at=datetime.now(UTC) + timedelta(seconds=60))
            session.add(ticket)
            await session.commit()
            consumed = await session.execute(update(ToolInvocationTicket).where(ToolInvocationTicket.id == ticket.id, ToolInvocationTicket.used_at.is_(None)).values(used_at=datetime.now(UTC)))
            await session.commit()
            if consumed.rowcount != 1:
                raise PreflightFailure("TICKET_CONSUME", "TICKET_ALREADY_USED", "Ticket was not consumed atomically.")
            stages.append({"stage": "TICKET_CREATE_CONSUME", "ok": True, "details": {"used_at_persisted": True}})
            result = {"ready": True, "sdk_version": sdk_version, "bridge_version": bridge.get("version", ""), "failed_stage": None, "error_code": None, "diagnostic_artifact": None, "stages": stages, "feature_flags": settings.feature_flags}
            self._last_result = result
            if run_id:
                self._ready_runs.add(run_id)
            return result
        except PreflightFailure as error:
            result = await self._failure(settings, diagnostic_id, stages, error, run_id)
            return result
        except Exception as error:  # infrastructure diagnostics must be stable
            result = await self._failure(settings, diagnostic_id, stages, PreflightFailure(stages[-1]["stage"] if stages else "BRIDGE_HEALTH", "MCP_BACKEND_UNREACHABLE", str(error)), run_id)
            return result
        finally:
            if run is not None:
                # The MCP subprocess can mint tickets before the handshake
                # returns. Delete by run scope, not only by the local ticket
                # variable, so a failed tools/list cannot strand a Ticket
                # that prevents the temporary Lease/Run from being removed.
                await session.execute(
                    delete(ToolInvocationTicket)
                    .where(ToolInvocationTicket.run_id == run.id)
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
            if lease is not None:
                await session.delete(lease)
            if attempt is not None:
                await session.delete(attempt)
            if run is not None:
                await session.delete(run)
            if challenge is not None:
                await session.delete(challenge)
            try:
                await session.flush()
                await session.commit()
            except Exception:
                await session.rollback()
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)

    async def _bridge_health(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
            response.raise_for_status()
            body = response.json()
            if not body.get("ctfctl_mcp_ready"):
                raise PreflightFailure("BRIDGE_HEALTH", "MCP_PROCESS_START_FAILED", "Bridge reports ctfctl MCP unavailable.")
            return body

    async def _mcp_stdio_handshake(self, settings, temp_root: Path, challenge: Challenge, run: SolveRun, attempt: RunAttempt, lease: RunExecutionLease) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        repo_root = Path(__file__).resolve().parents[3]
        program = repo_root / "codex-bridge" / "dist" / "ctfctl-mcp.js"
        if not program.is_file():
            raise PreflightFailure("MCP_PROCESS_START", "MCP_PROCESS_START_FAILED", "Compiled ctfctl MCP entrypoint is missing.")
        scope = {
            "run_id": run.id,
            "challenge_id": challenge.id,
            "workspace_root": str(temp_root),
            "allowed_hosts": list(challenge.allowed_hosts or []),
            "attempt_id": attempt.id,
            "lease_token": lease.lease_token,
            "master_lease_token": lease.lease_token,
            "thread_id": "codex-preflight",
            "model_turn_id": "codex-preflight",
        }
        env = os.environ.copy()
        env.update({
            "CTFCTL_SCOPE": json.dumps(scope),
            "CTFCTL_BACKEND_URL": os.getenv("CTFCTL_BACKEND_URL", "http://127.0.0.1:8000"),
            "CTFCTL_ACCESS_KEY": settings.ctfctl_internal_access_key,
        })
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "workspace_tree", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "workspace_read", "arguments": {"path": "challenge.json"}}},
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", str(program)],
                input="".join(json.dumps(item) + "\n" for item in requests),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PreflightFailure("MCP_PROCESS_START", "MCP_PROCESS_START_FAILED", str(error)) from error
        responses: dict[int, dict[str, Any]] = {}
        for line in (completed.stdout or "").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                responses[item["id"]] = item
        initialize = responses.get(1)
        if completed.returncode != 0 or not initialize or "error" in initialize:
            raise PreflightFailure("MCP_INITIALIZE", "MCP_INITIALIZE_FAILED", (completed.stderr or "MCP initialize failed")[:1000])
        catalog_response = responses.get(2)
        if not catalog_response or "error" in catalog_response:
            raise PreflightFailure("MCP_TOOLS_LIST", "MCP_TOOL_CATALOG_FAILED", (catalog_response or {}).get("error", {}).get("message", "MCP tools/list failed"))
        tools = ((catalog_response.get("result") or {}).get("tools") or [])
        if not isinstance(tools, list):
            raise PreflightFailure("MCP_TOOLS_LIST", "MCP_TOOL_CATALOG_FAILED", "MCP tools/list returned an invalid catalog.")
        for request_id, stage in ((3, "WORKSPACE_TREE"), (4, "WORKSPACE_READ")):
            response = responses.get(request_id)
            if not response or "error" in response:
                raise PreflightFailure(stage, "MCP_BACKEND_UNREACHABLE", f"MCP {stage} call failed.")
        return [
            {"stage": "MCP_PROCESS_START", "ok": True, "details": {"program": str(program)}},
            {"stage": "MCP_INITIALIZE", "ok": True, "details": {"protocol": initialize.get("result", {}).get("protocolVersion")}},
            {"stage": "MCP_TOOLS_LIST", "ok": True, "details": {"tool_count": len(tools)}},
            {"stage": "WORKSPACE_TREE", "ok": True, "details": {"workspace": str(temp_root)}},
            {"stage": "WORKSPACE_READ", "ok": True, "details": {"path": "challenge.json"}},
        ], tools

    @staticmethod
    def _sdk_version() -> str:
        path = Path(__file__).resolve().parents[3] / "codex-bridge" / "node_modules" / "@openai" / "codex-sdk" / "package.json"
        return str(json.loads(path.read_text(encoding="utf-8")).get("version", "")) if path.exists() else ""

    @staticmethod
    def _cli_version() -> str:
        try:
            result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=8, check=False)
            return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    async def _failure(self, settings, diagnostic_id: str, stages: list[dict[str, Any]], error: "PreflightFailure", run_id: str | None) -> dict[str, Any]:
        artifact_root = Path(settings.workspace_root).resolve() / "diagnostics"
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact = artifact_root / f"codex-preflight-{diagnostic_id}.json"
        payload = {"diagnostic_id": diagnostic_id, "failed_stage": error.stage, "error_code": error.code, "message": error.message[:4000], "stages": stages, "timestamp": datetime.now(UTC).isoformat()}
        artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {"ready": False, "sdk_version": self._sdk_version(), "bridge_version": "", "failed_stage": error.stage, "error_code": error.code, "diagnostic_artifact": str(artifact), "stages": stages, "feature_flags": settings.feature_flags}
        self._last_result = result
        if run_id:
            self._ready_runs.discard(run_id)
        return result


class PreflightFailure(RuntimeError):
    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage, self.code, self.message = stage, code, message


codex_preflight_service = CodexPreflightService()
