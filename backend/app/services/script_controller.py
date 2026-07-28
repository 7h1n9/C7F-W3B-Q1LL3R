"""Controller-owned bounded extraction fallback.

Once a boolean oracle is durable, the model is no longer responsible for
choosing the next low-level action.  This controller owns the fixed
CREATE -> VALIDATE -> EXECUTE sequence and records its lifecycle separately
from the result artifact.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
import re

from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.run import Artifact, RunAttempt, RunExecutionLease, ScriptRecord, SolveRun, ToolCall
from app.schemas.multi_agent import ScriptProposalContract
from app.services.events import event_service
from app.services.runner_client import runner_client
from app.services.solver_state import solver_state_service
from app.tools.gateway import tool_gateway


def _scope(run: SolveRun, challenge: Challenge, attempt: RunAttempt, lease: RunExecutionLease) -> Any:
    from app.api.v1.ctfctl import Scope

    return Scope(
        run_id=run.id,
        challenge_id=challenge.id,
        workspace_root=run.workspace_path,
        allowed_hosts=list(challenge.allowed_hosts or []),
        attempt_id=attempt.id,
        lease_token=lease.lease_token,
        master_lease_token=lease.lease_token,
        thread_id="script-fallback-controller",
        model_turn_id=run.active_turn_id,
        turn_id=run.active_turn_id,
    )


def _script_source(target_url: str, request_spec: dict, max_requests: int) -> str:
    # The target and request are supplied as bounded argv values.  The script
    # contains no challenge-source knowledge and never reads the workspace.
    request_json = json.dumps(request_spec or {}, ensure_ascii=False, separators=(",", ":"))
    target_json = json.dumps(target_url, ensure_ascii=False)
    return f'''import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request

TARGET = {target_json}
REQUEST = json.loads({request_json!r})
MAX_REQUESTS = {int(max_requests)}
JOB_ID = sys.argv[1] if len(sys.argv) > 1 else "unknown"
out_dir = "outputs/scripts/" + JOB_ID
result_path = out_dir + "/result.json"

def main():
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    method = str(REQUEST.get("method") or "GET").upper()
    body = REQUEST.get("body")
    data = None
    if isinstance(body, dict):
        data = urllib.parse.urlencode(body).encode()
    headers = {{"User-Agent": "bounded-script-controller/1"}}
    request = urllib.request.Request(TARGET, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(200000).decode("utf-8", "replace")
        status_code = int(response.status)
    candidates = sorted(set(re.findall(r"(?i)(?:[a-z0-9_-]+\\{{[^{{}}\\r\\n]{{1,256}}\\}})", raw)))
    payload = {{
        "status": "COMPLETED",
        "structured_result": {{
            "status": "COMPLETED",
            "status_code": status_code,
            "requests_sent": 1,
            "max_requests": MAX_REQUESTS,
            "response_excerpt": raw[:4000],
            "candidate_values": candidates,
            "extraction_mode": "BOUNDED_SCRIPT_EXTRACTION",
        }},
    }}
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def validate_script_proposal(payload: dict) -> ScriptProposalContract:
    """Reject prose-only or incomplete model script proposals."""
    if not payload.get("script_content"):
        raise DomainError("SCRIPT_CONTENT_MISSING", "CREATE_BOUNDED_SCRIPT requires non-empty script_content.", status_code=422)
    try:
        return ScriptProposalContract.model_validate(payload)
    except Exception as error:
        raise DomainError("SCRIPT_PROPOSAL_INVALID", "The bounded script proposal does not match the execution contract.", {"error": str(error)[:2000]}, 422) from error


class ExtractionFallbackController:
    required_sequence = ("CREATE_SCRIPT", "VALIDATE_SCRIPT", "EXECUTE_SCRIPT")

    async def _record_status(self, session, run: SolveRun, record: ScriptRecord, status: str, **fields) -> None:
        record.status = status
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        await session.commit()
        await event_service.append(session, run.id, "script.record.status", {"script_id": record.id, "status": status, **{key: value for key, value in fields.items() if key in {"validation_error", "execution_error"}}})

    async def should_run(self, session, run: SolveRun, challenge: Challenge) -> bool:
        state = await solver_state_service.load(session, run.id)
        checkpoint = run.recovery_checkpoint_json or {}
        ledger = state.capability_ledger_json if state else {}
        boolean_confirmed = any(key in ledger for key in ("matched_boolean_oracle_confirmed", "boolean_oracle_confirmed", "department_boolean_sqli_confirmed"))
        if not boolean_confirmed or run.status in {"COMPLETED_SOLVED", "CANCELLED", "FAILED_ENGINE", "FAILED_RUNNER"}:
            return False
        verified = await session.scalar(select(ScriptRecord.id).where(ScriptRecord.run_id == run.id, ScriptRecord.status.in_(("COMPLETED", "PARTIAL"))))
        if verified:
            return False
        existing = await session.scalar(select(ScriptRecord.id).where(ScriptRecord.run_id == run.id, ScriptRecord.status.in_(("CREATED", "VALIDATING", "VALIDATED", "RUNNING"))))
        if existing:
            return False
        # A preferred extractor is allowed to run first when the effective
        # Attempt manifest really advertises it.  Missing/stale schema falls
        # through to the controller-owned script path.
        preferred = set((checkpoint.get("next_required_action") or {}).get("preferred_tools") or {"boolean_config_extract"})
        manifest_tools = set()
        attempt_id = None
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
        if lease:
            attempt_id = lease.attempt_id
        if attempt_id:
            from app.models.run import AttemptToolManifest
            manifest = await session.scalar(select(AttemptToolManifest).where(AttemptToolManifest.attempt_id == attempt_id))
            manifest_tools = set(manifest.effective_tools or []) if manifest else set()
        preferred_available = bool(preferred & manifest_tools) and not (checkpoint.get("mcp_schema_degraded") or checkpoint.get("tool_catalog_drift"))
        # Two pure replans are the controller's emission watchdog boundary:
        # at that point a preferred action may still be advertised, but the
        # model has not emitted a durable action and the script fallback owns
        # the next step.
        return (not preferred_available) or bool(checkpoint.get("force_script_fallback")) or int(state.no_progress_count or 0) >= 2

    async def run(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, lease: RunExecutionLease) -> dict:
        if not await self.should_run(session, run, challenge):
            return {"status": "SKIPPED", "reason": "PREFERRED_ACTION_AVAILABLE_OR_NOT_TRIGGERED"}
        state = await solver_state_service.load(session, run.id)
        checkpoint = run.recovery_checkpoint_json or {}
        request_spec = dict(checkpoint.get("request_spec") or {"method": "GET"})
        max_requests = min(20, max(1, int((checkpoint.get("oracle") or {}).get("max_requests") or 1)))
        script_path = "scripts/bounded_extraction.py"
        content = _script_source(challenge.target_url, request_spec, max_requests)
        design_card = {
            "controller": "ExtractionFallbackController",
            "objective": "Extract and materialize a candidate through the confirmed bounded oracle.",
            "network_mode": "target_allowlist",
            "allowed_hosts": list(challenge.allowed_hosts or []),
            "max_requests": max_requests,
            "max_runtime_seconds": 60,
            "checkpoint": "outputs/scripts/{job_id}/checkpoint.json",
            "resume": "rerun with the same bounded request contract only",
            "forbidden_knowledge": ["challenge_source", "database_schema", "historical_flags"],
        }
        from app.api.v1.ctfctl import WriteRequest, workspace_write_file

        scope = _scope(run, challenge, attempt, lease)
        write_payload = WriteRequest(
            scope=scope,
            required_action=True,
            required_action_kind="workspace_write_file",
            path=script_path,
            content=content,
            overwrite=True,
        )
        await workspace_write_file(write_payload, get_settings().ctfctl_internal_access_key, session)
        digest = hashlib.sha256(content.encode()).hexdigest()
        record = ScriptRecord(
            run_id=run.id,
            path=script_path,
            script_path=script_path,
            sha256=digest,
            source="CONTROLLER_GENERATED",
            assistance_level=run.assistance_level or "AUTONOMOUS",
            assumption_provenance_json=["controller_checkpoint", "confirmed_boolean_oracle"],
            design_card_json=design_card,
            objective=design_card["objective"],
            network_mode="target_allowlist",
            allowed_hosts_json=list(challenge.allowed_hosts or []),
            max_requests=max_requests,
            max_runtime_seconds=60,
            status="CREATED",
        )
        session.add(record)
        await session.commit()
        await event_service.append(session, run.id, "script.record.created", {"script_id": record.id, "path": script_path, "sha256": digest, "sequence": list(self.required_sequence)})

        await self._record_status(session, run, record, "VALIDATING")
        try:
            validation = await tool_gateway.invoke(
                session, run, challenge, "sandbox_exec",
                {"executable": "file", "args": [], "cwd": "scratch", "network_mode": "none", "validation_mode": "script", "path": script_path},
                execution_layer="script_controller", logical_kind="SCRIPT_VALIDATION",
                required_action=True, required_action_kind="sandbox_exec",
            )
        except Exception as error:
            await self._record_status(session, run, record, "FAILED", validation_error=str(error)[:4000])
            return {"status": "FAILED", "error_code": "SCRIPT_VALIDATION_FAILED", "error": str(error)}
        validation_status = str((validation.get("model_view") or {}).get("status") or validation.get("status") or "")
        if validation_status not in {"COMPLETED", "VALIDATED"}:
            await self._record_status(session, run, record, "FAILED", validation_error=json.dumps(validation, ensure_ascii=False)[:4000])
            return {"status": "FAILED", "error_code": "SCRIPT_VALIDATION_FAILED", "result": validation}
        await self._record_status(session, run, record, "VALIDATED")

        await self._record_status(session, run, record, "RUNNING")
        try:
            result = await tool_gateway.invoke(
                session, run, challenge, "script_run",
                {"path": script_path, "interpreter": "python", "args": ["bounded-controller"], "network_mode": "target_allowlist", "timeout_seconds": 60, "design_card": design_card, "assumption_provenance": record.assumption_provenance_json},
                execution_layer="script_controller", logical_kind="SCRIPT_EXECUTION",
                required_action=True, required_action_kind="script_run",
            )
        except Exception as error:
            await self._record_status(session, run, record, "FAILED", execution_error=str(error)[:4000])
            return {"status": "FAILED", "error_code": "SCRIPT_EXECUTION_FAILED", "error": str(error)}
        result_status = str((result.get("model_view") or {}).get("status") or result.get("status") or "FAILED")
        lifecycle = "COMPLETED" if result_status == "COMPLETED" else "PARTIAL" if result_status == "PARTIAL" else "FAILED"
        latest_call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.tool_name == "script_run").order_by(ToolCall.created_at.desc()))
        latest_artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == (latest_call.id if latest_call else "")).order_by(Artifact.created_at.desc()))
        await self._record_status(session, run, record, lifecycle, tool_call_id=latest_call.id if latest_call else None, result_artifact_id=latest_artifact.id if latest_artifact else None, execution_error=None if lifecycle != "FAILED" else json.dumps(result, ensure_ascii=False)[:4000])
        return {"status": lifecycle, "script_id": record.id, "result": result}


script_fallback_controller = ExtractionFallbackController()
