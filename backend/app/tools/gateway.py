import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus
from app.services.events import event_service
from app.services.flags import flag_service
from app.tools.policy import enforce_tool_policy
from app.tools.registry import load_tool_definitions


class ToolGateway:
    async def invoke(self, session: AsyncSession, run: SolveRun, challenge: Challenge, name: str, arguments: dict) -> dict:
        definitions = load_tool_definitions()
        definition = definitions.get(name)
        if not definition or not definition.enabled:
            raise DomainError("TOOL_NOT_AVAILABLE", "Requested tool is not enabled.", {"tool": name}, 404)
        if run.status != RunStatus.EXECUTING:
            raise DomainError("RUN_TOOL_NOT_ALLOWED", "Tools can only execute while the run is in EXECUTING state.", {"current_state": run.status}, 409)
        arguments = definition.validate_arguments(arguments)
        enforce_tool_policy(name, arguments, challenge.allowed_hosts)
        call = ToolCall(run_id=run.id, tool_name=name, arguments_json=arguments, status="REQUESTED", started_at=datetime.now(UTC))
        session.add(call); await session.commit(); await session.refresh(call)
        await event_service.append(session, run.id, "tool.requested", {"tool_call_id": call.id, "tool": name})
        call.status = "STARTED"
        await session.commit()
        await event_service.append(session, run.id, "tool.started", {"tool_call_id": call.id, "tool": name})
        try:
            async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
                headers = {"X-Runner-Token": get_settings().runner_api_token}
                response = await client.post(f"{get_settings().runner_url}/api/v1/jobs", headers=headers, json={"run_id": run.id, "workspace_path": run.workspace_path, "allowed_hosts": challenge.allowed_hosts, "tool": name, "arguments": arguments})
                response.raise_for_status()
                job_id = response.json()["job_id"]
                for _ in range(70):
                    status_response = await client.get(f"{get_settings().runner_url}/api/v1/jobs/{job_id}", headers=headers)
                    status_response.raise_for_status()
                    result = status_response.json()
                    if result["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                        result = {**result.get("result", {}), "job_id": result.get("job_id"), "status": result["status"], "error": result.get("error")}
                        break
                    await asyncio.sleep(0.5)
                else:
                    result = {"job_id": job_id, "status": "FAILED", "summary": "Runner polling timed out"}
        except httpx.HTTPError as error:
            result = {"status": "FAILED", "summary": "Runner request failed", "error": str(error)}
        call.status, call.runner_job_id, call.finished_at = ("COMPLETED" if result.get("status") == "COMPLETED" else "FAILED"), result.get("job_id"), datetime.now(UTC)
        relative = str(result.get("artifact_path") or "")
        root = Path(run.workspace_path).resolve()
        target = (root / relative).resolve()
        if not relative or root not in target.parents or not target.is_file():
            relative = f"outputs/runner_error_{call.id}.json"
            target = (root / relative).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        raw = target.read_bytes()
        artifact = Artifact(run_id=run.id, tool_call_id=call.id, artifact_type="tool_output", file_path=relative.replace("\\", "/"), size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), summary=str(result.get("summary", ""))[:1000])
        session.add(artifact); await session.flush()
        observation = Observation(run_id=run.id, tool_call_id=call.id, artifact_id=artifact.id, observation_type="tool_result", summary=artifact.summary, facts_json={"tool": name, "ok": result.get("status") == "COMPLETED"})
        session.add(observation); await session.commit()
        await event_service.append(session, run.id, "artifact.created", {"artifact_id": artifact.id, "path": artifact.file_path, "size": artifact.size, "sha256": artifact.sha256})
        await flag_service.extract_candidates(session, run, challenge, artifact, raw.decode(errors="replace"))
        event_type = "tool.completed" if result.get("status") == "COMPLETED" else "tool.failed"
        await event_service.append(session, run.id, event_type, {"tool_call_id": call.id, "result": result})
        return result


tool_gateway = ToolGateway()
