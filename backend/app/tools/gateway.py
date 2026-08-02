import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import (
    AnalysisReview,
    ApprovedAction,
    EvidenceLedger,
    VerifiedFact,
)
from app.models.run import (
    SCRIPT_RECORD_STATUSES,
    AgentTurn,
    Artifact,
    Hypothesis,
    Observation,
    RunExecutionLease,
    ScriptRecord,
    SolveRun,
    ToolCall,
)
from app.schemas.tool import ToolArtifactRef, ToolExecutionResult, ToolModelView
from app.services.assistance import assistance_level
from app.services.compaction_scheduler import compaction_scheduler
from app.services.effective_logical_tool_calls import effective_logical_tool_call_service
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.infrastructure import clear_failure, record_failure
from app.services.run_budget_guard import run_budget_guard
from app.services.runner_client import runner_client
from app.services.solver_state import solver_state_service
from app.services.sql_provenance import validate_sql_expression_provenance
from app.services.tool_argument_adapter import adapt_arguments
from app.services.tool_invocation_coordinator import tool_invocation_coordinator
from app.services.tool_permissions import effective_tools_for
from app.services.workspace_sync import workspace_sync_service
from app.tools.policy import enforce_tool_policy


_TOOL_SUCCESS_STATUSES = {"COMPLETED", "SUCCESS", "CACHED"}
_TOOL_EXECUTION_COMPLETED_STATUSES = _TOOL_SUCCESS_STATUSES | {"NO_FACT"}


def _result_contract_status(result: dict) -> str:
    return str(result.get("result_status") or result.get("status") or "FAILED").upper()


def _metadata_result_has_required_fact(arguments: dict, result: dict) -> bool:
    """Return whether a COMPLETED metadata result contains this stage's fact."""
    structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
    extracted = structured.get("extracted_facts") if isinstance(structured.get("extracted_facts"), dict) else {}
    stage = str(arguments.get("stage") or structured.get("stage") or "").lower()
    required = {
        "version": ("version",),
        "version_comment": ("version_comment",),
        "database": ("current_database",),
        "tables": ("tables",),
        "columns": ("columns",),
    }.get(stage)
    if not required:
        return False
    values = {key: extracted.get(key, structured.get(key)) for key in required}
    return all(bool(value) for value in values.values())
from app.tools.registry import load_tool_definitions

_SECRET_KEYS = {"token", "password", "passwd", "secret", "api_key", "authorization", "cookie", "set-cookie"}


def is_recon_sql_payload(arguments: dict) -> bool:
    """Detect SQL exploit syntax before a Recon request reaches the Runner."""
    flattened = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True).lower()
    markers = ("1=1", "1 = 1", "1%3d1", "1=2", "1 = 2", "union select", "union+select", "select%20", "boolean extraction")
    return any(marker in flattened for marker in markers)


def _redact_arguments(value):
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if str(key).lower() in _SECRET_KEYS else _redact_arguments(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value]
    return value


class ToolGateway:
    async def _validate_sql_sources(self, session: AsyncSession, run: SolveRun, arguments: dict) -> None:
        """Resolve provenance IDs to this Run's durable records."""
        evidence_ids = {str(item) for item in arguments.get("supporting_evidence_ids") or []}
        fact_ids = {str(item) for item in arguments.get("supporting_fact_ids") or []}
        evidence = list((await session.scalars(select(EvidenceLedger).where(EvidenceLedger.run_id == run.id, EvidenceLedger.id.in_(evidence_ids)))).all()) if evidence_ids else []
        facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.id.in_(fact_ids)))).all()) if fact_ids else []
        hypothesis = await session.scalar(select(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.id == str(arguments.get("source_hypothesis_id") or "")))
        review = await session.get(AnalysisReview, str(arguments.get("approved_analysis_review_id") or ""))
        if len(evidence) != len(evidence_ids) or len(facts) != len(fact_ids) or hypothesis is None or review is None:
            raise DomainError(
                "SQL_EXPRESSION_PROVENANCE_REQUIRED",
                "SQL expression provenance must reference Evidence, VerifiedFact, Hypothesis, and AnalysisReview rows in this Run.",
                {"run_id": run.id, "evidence_ids": sorted(evidence_ids), "fact_ids": sorted(fact_ids)},
                422,
            )

    async def _ensure_script_record(self, session: AsyncSession, run: SolveRun, challenge: Challenge, arguments: dict) -> ScriptRecord:
        """Create the exploit-script lifecycle before a Runner job starts.

        ``python_run`` deliberately never calls this helper.  A generic
        ``script_run`` must be auditable even when deployment validation
        blocks it before a ToolCall or Runner Job exists.
        """
        path = str(arguments.get("path") or "")
        existing = await session.scalar(
            select(ScriptRecord)
            .where(ScriptRecord.run_id == run.id, ScriptRecord.path == path)
            .order_by(ScriptRecord.created_at.desc())
        )
        if existing is None or existing.status in {"COMPLETED", "PARTIAL", "FAILED", "BLOCKED_DEPLOYMENT", "CANCELLED"}:
            provenance = arguments.get("assumption_provenance") or []
            record = ScriptRecord(
                run_id=run.id,
                path=path,
                script_path=path,
                sha256=str(arguments.get("script_sha256") or ""),
                source="MODEL_GENERATED",
                assistance_level=assistance_level(provenance),
                assumption_provenance_json=provenance,
                design_card_json=arguments.get("design_card") or {},
                objective=str((arguments.get("design_card") or {}).get("objective") or ""),
                network_mode=str(arguments.get("network_mode") or "none"),
                allowed_hosts_json=list(challenge.allowed_hosts or []),
                max_requests=int(arguments.get("max_requests") or 0),
                max_runtime_seconds=int(arguments.get("timeout_seconds") or 60),
                status="CREATED",
            )
            session.add(record)
        else:
            record = existing
            record.status = "CREATED"
            record.validation_error = None
            record.execution_error = None
        await session.commit()
        await event_service.append(session, run.id, "script.record.status", {"script_id": record.id, "status": record.status, "path": path})
        return record

    async def _set_script_record_status(self, session: AsyncSession, run: SolveRun, record: ScriptRecord, status: str, **fields) -> None:
        if status not in SCRIPT_RECORD_STATUSES:
            raise DomainError("SCRIPT_STATUS_INVALID", "Unknown ScriptRecord lifecycle status.", {"status": status}, 500)
        record.status = status
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        await session.commit()
        await event_service.append(session, run.id, "script.record.status", {"script_id": record.id, "status": status, **{key: value for key, value in fields.items() if key in {"validation_error", "execution_error"}}})

    async def invoke(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, name: str, arguments: dict,
        *, logical_tool_call_id: str | None = None, parent_tool_call_id: str | None = None,
        execution_layer: str = "gateway", turn_id: str | None = None,
        provider_tool_name: str | None = None, logical_kind: str = "TOOL",
        required_action: bool = False,
        required_action_kind: str | None = None,
        agent_task_id: str | None = None,
        agent_role: str | None = None,
        task_lease_token: str | None = None,
        approved_action_id: str | None = None,
    ) -> dict:
        definition = load_tool_definitions().get(name)
        if not definition or not definition.enabled:
            raise DomainError(
                "TOOL_NOT_AVAILABLE", "Requested tool is not enabled.", {"tool": name}, 404
            )
        # A model turn can arrive while the Run is moving through one of the
        # short-lived execution stages.  Only terminal/explicit pause states
        # are rejected here; attempt/lease freshness is checked in one place.
        coordinated = await tool_invocation_coordinator.validate(
            session, run, agent_task_id=agent_task_id, task_lease_token=task_lease_token,
            tool_name=name, agent_role=agent_role, approved_action_id=approved_action_id,
            arguments=arguments,
        )
        if agent_task_id and agent_role and coordinated["task"].agent_role != agent_role:
            raise DomainError("AGENT_SCOPE_INVALID", "The task role does not match the tool-call scope.", {"agent_task_id": agent_task_id, "agent_role": agent_role}, 403)
        lease = coordinated["lease"]
        permitted_tools = await effective_tools_for(session, run, challenge)
        metadata = challenge.metadata_json or {}
        if metadata.get("adapter") == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql" and name == "sqlite_metadata_discovery":
            raise DomainError(
                "TOOL_NOT_ALLOWED_FOR_DBMS",
                "SQLite metadata discovery is disabled for the MySQL asset-warranty challenge.",
                {"adapter": "asset_warranty", "dbms": "mysql", "required_tool": "mysql_metadata_discovery"},
                422,
            )
        if name not in permitted_tools:
            raise DomainError(
                "TOOL_NOT_ALLOWED_FOR_CHALLENGE",
                "This tool is not allowed by the current role or challenge limits.",
                {"tool": name, "challenge_type": challenge.challenge_type},
                422,
            )
        if name == "http_request":
            state = await solver_state_service.load(session, run.id)
            ledger = state.capability_ledger_json if state else {}
            confirmed = any(key in ledger for key in ("sql_injection_confirmed", "sqlmap_extraction_completed"))
            if confirmed and not bool(arguments.get("final_verification")):
                raise DomainError(
                    "AUTOMATION_REQUIRED",
                    "Confirmed SQL injection must use a bounded automation tool; HTTP is reserved for final verification.",
                    {"recommended_tools": ["sql_boolean_compare", "sqlmap_run", "script_run"], "final_verification": True},
                    422,
                )
        script_record: ScriptRecord | None = None
        if name == "script_run":
            script_record = await self._ensure_script_record(session, run, challenge, arguments)
            await self._set_script_record_status(session, run, script_record, "VALIDATING")
        if name == "script_run" and str(arguments.get("network_mode") or "none") == "target_allowlist":
            from app.models.run import AttemptToolManifest
            manifest = await session.scalar(select(AttemptToolManifest).where(AttemptToolManifest.attempt_id == lease.attempt_id))
            contract = (manifest.tool_capabilities_json or {}).get("script_run", {}) if manifest else {}
            if manifest is None or ("target_allowlist" not in set(contract.get("supported_network_modes") or []) or not contract.get("target_allowlist_enforced") or not (manifest.network_enforcement_json or {}).get("target_allowlist_enforced")):
                run.status = "PAUSED_DEPLOYMENT"
                run.last_error_code = "SCRIPT_TARGET_NETWORK_UNAVAILABLE"
                run.last_error_message = "Attempt manifest does not prove target allowlist enforcement."
                if script_record is not None:
                    await self._set_script_record_status(session, run, script_record, "BLOCKED_DEPLOYMENT", execution_error=run.last_error_message)
                await session.commit()
                raise DomainError("SCRIPT_TARGET_NETWORK_UNAVAILABLE", run.last_error_message, {"status": run.status}, 503, stage="NETWORK_POLICY", retryable=False)
        arguments = adapt_arguments(name, arguments, challenge)
        if agent_task_id and agent_role == "RECON":
            if is_recon_sql_payload(arguments):
                raise DomainError(
                    "RECON_ACTION_OUT_OF_SCOPE",
                    "Recon may establish HTTP and business baselines but may not execute SQL injection experiments.",
                    {"agent_task_id": agent_task_id, "redirect": "PLANNER_EXPLOIT_PROPOSAL"}, 422,
                )
        try:
            arguments = definition.validate_arguments(arguments)
        except DomainError as error:
            if error.code == "TOOL_INVALID_ARGUMENT":
                details = dict(error.details or {})
                details.update({"missing_fields": details.get("errors", []), "unknown_fields": [], "expected_schema": definition.parameters, "corrected_example": adapt_arguments(name, arguments), "available_operations": [name]})
                if approved_action_id:
                    approved = await session.get(ApprovedAction, approved_action_id)
                    if approved is not None:
                        approved.status = "REJECTED"
                        approved.compile_status = "REJECTED"
                        approved.compile_error_json = {"code": error.code, "details": details}
                    run.status = "PAUSED_CHECKPOINT"
                    run.last_error_code = "TOOL_INVALID_ARGUMENT"
                    run.last_error_message = error.message[:4000]
                    await session.commit()
                raise DomainError(error.code, error.message, details, error.status_code) from error
            raise
        enforce_tool_policy(name, arguments, challenge.allowed_hosts)
        verified_schema = False
        if "config.value" in str(arguments.get("target_expression") or "").lower():
            from app.models.multi_agent import VerifiedFact
            schema_facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.id.in_(arguments.get("supporting_fact_ids") or [])))).all())
            verified_schema = any(str(item.fact_type).upper() in {"SQL_SCHEMA", "SQL_TABLE", "SQL_COLUMN", "SCHEMA"} and item.promotion_status == "VERIFIED" for item in schema_facts)
        if name in {"boolean_config_extract", "mysql_metadata_discovery", "sqlite_metadata_discovery"}:
            await self._validate_sql_sources(session, run, arguments)
            validate_sql_expression_provenance(arguments, verified_schema=verified_schema)
        elif name == "sqlmap_run" and str(arguments.get("action") or "detect") != "detect":
            await self._validate_sql_sources(session, run, arguments)
            validate_sql_expression_provenance(arguments, verified_schema=verified_schema)
        elif name == "script_run" and arguments.get("target_expression"):
            await self._validate_sql_sources(session, run, arguments)
            validate_sql_expression_provenance(arguments, verified_schema=verified_schema)
        if script_record is not None:
            # The request has passed the Backend contract.  Runner still
            # performs the authoritative static validation before execution.
            await self._set_script_record_status(session, run, script_record, "VALIDATED")
        # Reserve only after policy and argument validation.  Rejected model
        # requests must not consume a durable tool budget slot.
        turn_id = turn_id or run.active_turn_id
        turn_started_at = None
        if turn_id:
            turn_started_at = await session.scalar(select(AgentTurn.turn_started_at).where(AgentTurn.id == turn_id, AgentTurn.run_id == run.id))
        await run_budget_guard.enforce(
            session,
            run,
            attempt_id=lease.attempt_id,
            turn_id=turn_id,
            required_action=required_action,
            required_action_kind=required_action_kind or name,
        )
        if name == "file_read":
            cached = await self._cached_file_read(session, run, arguments)
            if cached is not None:
                await run_budget_guard.release(session, run, turn_id=turn_id, required_action=required_action, required_action_kind=required_action_kind or name)
                await session.commit()
                await event_service.append(
                    session,
                    run.id,
                    "tool.read_deduplicated",
                    {"tool": name, "code": "FILE_RANGE_ALREADY_AVAILABLE", "path": arguments.get("path")},
                )
                return cached if isinstance(cached, dict) else cached.model_dump()
        if script_record is not None:
            await self._set_script_record_status(session, run, script_record, "RUNNING")
        provider_call_id = str(uuid.uuid4())
        logical_tool_call_id = logical_tool_call_id or effective_logical_tool_call_service.build_mcp_id(
            run.id, lease.attempt_id, str(turn_id or "turn"), provider_call_id
        )
        call = ToolCall(
            run_id=run.id,
            tool_name=name,
            arguments_json=_redact_arguments(arguments),
            status="REQUESTED",
            started_at=datetime.now(UTC),
            logical_tool_call_id=logical_tool_call_id,
            parent_tool_call_id=parent_tool_call_id,
            execution_layer=execution_layer,
            counts_toward_budget=True,
            logical_kind=logical_kind,
            provider_tool_name=provider_tool_name or name,
            effective_tool_name=name,
            turn_id=turn_id,
            agent_task_id=agent_task_id,
            approved_action_id=approved_action_id,
            agent_role=agent_role,
            task_lease_token=task_lease_token,
        )
        session.add(call)
        if approved_action_id:
            approved = await session.get(ApprovedAction, approved_action_id)
            if approved is not None:
                approved.used_logical_calls = int(approved.used_logical_calls or 0) + 1
        await session.commit()
        await session.refresh(call)
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
        logical_id = str(call.logical_tool_call_id)
        logical = await effective_logical_tool_call_service.ensure(
            session,
            run,
            logical_tool_call_id=logical_id,
            tool_name=name,
            arguments=arguments,
            status="REQUESTED",
            started_at=call.started_at,
            attempt_id=lease.attempt_id if lease else None,
            counts_toward_budget=True,
            logical_kind=logical_kind,
            provider_tool_name=provider_tool_name or name,
            effective_tool_name=name,
            turn_id=turn_id,
            turn_started_at=turn_started_at,
        )
        # The gateway is also the execution boundary for controller-owned
        # multi-agent calls.  Keep the durable counters in sync here so those
        # calls are visible in reports and cannot appear as zero-tool Runs.
        run.tool_call_count = int(run.tool_call_count or 0) + 1
        run.run_total_logical_tool_calls = int(run.run_total_logical_tool_calls or 0) + 1
        run.attempt_logical_tool_calls = int(run.attempt_logical_tool_calls or 0) + 1
        await run_budget_guard.release(session, run, turn_id=turn_id, required_action=required_action, required_action_kind=required_action_kind or name)
        await effective_logical_tool_call_service.trace(
            session, logical, execution_layer=execution_layer, event_type="requested", external_id=call.id
        )
        await session.commit()
        await event_service.append(
            session, run.id, "tool.requested", {"tool_call_id": call.id, "logical_tool_call_id": call.logical_tool_call_id, "tool": name, "execution_layer": call.execution_layer}
        )
        call.status = "STARTED"
        logical.status = "STARTED"
        await effective_logical_tool_call_service.trace(
            session, logical, execution_layer=execution_layer, event_type="started", external_id=call.id
        )
        await session.commit()
        await event_service.append(
            session, run.id, "tool.started", {"tool_call_id": call.id, "logical_tool_call_id": call.logical_tool_call_id, "tool": name, "execution_layer": call.execution_layer}
        )
        try:
            # ctfctl can create a bounded scripts/*.py file during a turn.
            # The remote Runner has its own per-run workspace, so synchronize
            # that file immediately before python_run instead of treating a
            # missing remote copy as a request for arbitrary shell access.
            if name in {"python_run", "script_run", "sandbox_exec"}:
                await runner_client.sync_workspace(run.id, Path(run.workspace_path))
            job_id = await runner_client.create_job(
                run.id, challenge.allowed_hosts, name, arguments
            )
            try:
                result = await runner_client.wait_job(
                    job_id,
                    tool_timeout_seconds=min(600, int(arguments.get("timeout_seconds", 30))),
                )
            except TypeError as error:
                if "tool_timeout_seconds" not in str(error):
                    raise
                result = await runner_client.wait_job(job_id)
            contract_status = _result_contract_status(result)
            # Compatibility path for a pre-contract Runner: an empty
            # COMPLETED metadata response is a durable NO_FACT outcome, not a
            # RESULT_CONTRACT/FAILED outcome.
            if name == "mysql_metadata_discovery" and contract_status in {"COMPLETED", "SUCCESS"} and not _metadata_result_has_required_fact(arguments, result):
                result = {
                    **result,
                    "status": "NO_FACT",
                    "result_status": "NO_FACT",
                    "error_code": "MYSQL_METADATA_EMPTY_RESULT",
                    "summary": "mysql_metadata_discovery completed without a distinguishable metadata fact.",
                    "stage": str(arguments.get("stage") or result.get("stage") or "metadata").lower(),
                    "tool_execution_completed": True,
                    "retryable": True,
                }
            with contextlib.suppress(Exception):
                await workspace_sync_service.sync_from_runner(run.id, Path(run.workspace_path))
            if _result_contract_status(result) not in _TOOL_SUCCESS_STATUSES and not result.get("error_code"):
                error_text = str(result.get("error") or result.get("summary") or "").lower()
                if "not found" in error_text or "does not exist" in error_text:
                    result["error_code"] = "FILE_NOT_FOUND"
                elif "not installed" in error_text:
                    result["error_code"] = "TOOL_NOT_INSTALLED"
                elif "script" in error_text and "sync" in error_text:
                    result["error_code"] = "SCRIPT_NOT_SYNCED"
            if (
                name in {"file_read", "python_run", "script_run"}
                and _result_contract_status(result) not in _TOOL_SUCCESS_STATUSES
                and "not found" in str(result.get("error") or result.get("summary") or "").lower()
            ):
                # A Runner workspace can lag after attachment updates or a
                # bridge/thread rebuild. Reconcile its manifest and retry the
                # exact bounded read once before surfacing FILE_NOT_FOUND.
                await runner_client.sync_workspace(run.id, Path(run.workspace_path))
                retry_job_id = await runner_client.create_job(
                    run.id, challenge.allowed_hosts, name, arguments
                )
                try:
                    result = await runner_client.wait_job(
                        retry_job_id,
                        tool_timeout_seconds=min(600, int(arguments.get("timeout_seconds", 30))),
                    )
                except TypeError as error:
                    if "tool_timeout_seconds" not in str(error):
                        raise
                    result = await runner_client.wait_job(retry_job_id)
                if _result_contract_status(result) not in _TOOL_SUCCESS_STATUSES and name == "file_read":
                    result["error_code"] = "FILE_NOT_FOUND"
            if result.get("error_code") in {"TARGET_UNAVAILABLE", "BACKEND_UNAVAILABLE", "RUNNER_UNAVAILABLE", "TOOL_RESULT_DELIVERY_FAILED"}:
                record_failure(run, code=str(result["error_code"]), message=str(result.get("error") or result.get("summary") or result["error_code"]), stage=str(result.get("stage") or "EXECUTION"))
                await session.commit()
            if result.get("error_code") in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE"}:
                run.status = "PAUSED_DEPLOYMENT"
                run.last_error_code = str(result["error_code"])
                run.last_error_message = str(result.get("error") or result.get("summary") or result["error_code"])[:4000]
                await session.commit()
        except Exception as error:
            code = getattr(error, "code", None) or ("RUNNER_UNAVAILABLE" if isinstance(error, (ConnectionError, TimeoutError)) else "RUNNER_JOB_FAILED")
            result = {
                "status": "FAILED",
                "error_code": code,
                "summary": "Runner request failed",
                "error": str(getattr(error, "message", None) or error),
                "stage": getattr(error, "stage", "RUNNER"),
                "retryable": code == "RUNNER_UNAVAILABLE",
            }
            if code == "RUNNER_UNAVAILABLE":
                record_failure(run, code=code, message=result["error"], stage="RUNNER")
            await session.commit()
        contract_status = _result_contract_status(result)
        call.status, call.runner_job_id, call.finished_at = (
            ("COMPLETED" if contract_status in _TOOL_EXECUTION_COMPLETED_STATUSES else "FAILED"),
            result.get("job_id"),
            datetime.now(UTC),
        )
        logical.status = call.status
        logical.finished_at = call.finished_at
        root = Path(run.workspace_path).resolve()
        relative = str(result.get("artifact_path") or "")
        target = (root / relative).resolve()
        artifact: Artifact | None = None
        artifact_event_payload: dict | None = None
        if relative and root in target.parents:
            try:
                size, checksum = await runner_client.download_artifact(
                    run.id, relative, target, result.get("artifact_sha256")
                )
            except Exception as error:
                result = {
                    **result,
                    "status": "FAILED",
                    "error_code": "TOOL_RESULT_DELIVERY_FAILED",
                    "stage": "ARTIFACT_DOWNLOAD",
                    "tool_execution_completed": contract_status in _TOOL_EXECUTION_COMPLETED_STATUSES,
                    "summary": "Artifact download failed",
                    "error": str(error),
                }
                record_failure(run, code="TOOL_RESULT_DELIVERY_FAILED", message=str(error), stage="ARTIFACT_DOWNLOAD")
                await session.commit()
                relative = ""
        # file_read is an inspection operation. Its bounded content is already
        # carried by ToolModelView/Observation; do not create a new runner_error
        # artifact for every read of the same workspace file.
        if not relative and name == "file_read":
            structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
            candidate_path = str(structured.get("path") or "").replace("\\", "/")
            candidate = (root / candidate_path).resolve() if candidate_path else root
            if candidate_path and root in candidate.parents and candidate.is_file():
                checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
                artifact = await session.scalar(
                    select(Artifact)
                    .where(
                        Artifact.run_id == run.id,
                        Artifact.file_path == candidate_path,
                        Artifact.sha256 == checksum,
                    )
                    .order_by(Artifact.created_at.desc())
                )
                target, relative = candidate, candidate_path
        if not relative and name != "file_read":
            relative = f"outputs/runner_error_{call.id}.json"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            size, checksum = target.stat().st_size, hashlib.sha256(target.read_bytes()).hexdigest()
        if artifact is None and name != "file_read":
            artifact = Artifact(
                run_id=run.id,
                tool_call_id=call.id,
                artifact_type="tool_output",
                file_path=relative.replace("\\", "/"),
                size=size,
                sha256=checksum,
                summary=str(result.get("summary", ""))[:1000],
            )
            session.add(artifact)
            await session.flush()
            artifact_event_payload = {
                "artifact_id": artifact.id,
                "path": artifact.file_path,
                "size": artifact.size,
                "sha256": artifact.sha256,
            }
        if name == "script_run" and artifact is not None:
            provenance = arguments.get("assumption_provenance") or []
            level = assistance_level(provenance)
            existing_script = script_record or await session.scalar(
                select(ScriptRecord).where(ScriptRecord.run_id == run.id, ScriptRecord.path == str(arguments.get("path") or artifact.file_path)).order_by(ScriptRecord.created_at.desc())
            )
            if existing_script is None:
                existing_script = ScriptRecord(
                    run_id=run.id,
                    artifact_id=artifact.id,
                    path=str(arguments.get("path") or artifact.file_path),
                    script_path=str(arguments.get("path") or artifact.file_path),
                    sha256=str(arguments.get("script_sha256") or ""),
                    source="MODEL_GENERATED",
                    assistance_level=level,
                    assumption_provenance_json=provenance,
                    design_card_json=arguments.get("design_card") or {},
                    objective=str((arguments.get("design_card") or {}).get("objective") or ""),
                    network_mode=str(arguments.get("network_mode") or "none"),
                    allowed_hosts_json=list(challenge.allowed_hosts or []),
                    max_requests=int(arguments.get("max_requests") or 0),
                    max_runtime_seconds=int(arguments.get("timeout_seconds") or 60),
                    status="COMPLETED" if result.get("status") == "COMPLETED" and isinstance(result.get("structured_result"), dict) and str(result.get("structured_result", {}).get("status") or "") == "COMPLETED" else "PARTIAL" if result.get("status") == "PARTIAL" else "FAILED",
                    tool_call_id=call.id,
                    result_artifact_id=artifact.id,
                )
                session.add(existing_script)
            else:
                existing_script.artifact_id = artifact.id
                existing_script.status = "COMPLETED" if result.get("status") == "COMPLETED" and isinstance(result.get("structured_result"), dict) and str(result.get("structured_result", {}).get("status") or "") == "COMPLETED" else "PARTIAL" if result.get("status") == "PARTIAL" else "FAILED"
                existing_script.execution_error = None if existing_script.status in {"COMPLETED", "PARTIAL"} else str(result.get("error") or result.get("summary") or "")[:4000]
                existing_script.result_artifact_id = artifact.id
                existing_script.tool_call_id = call.id
                if not existing_script.sha256 and arguments.get("script_sha256"):
                    existing_script.sha256 = str(arguments["script_sha256"])
            if level == "ANSWER_GUIDED" or (level == "EVIDENCE_GUIDED" and run.assistance_level == "AUTONOMOUS"):
                run.assistance_level = level
            current_sources = list(run.assistance_sources_json or [])
            for source in provenance:
                if source not in current_sources:
                    current_sources.append(source)
            run.assistance_sources_json = current_sources
        if script_record is not None and (artifact is None or name != "script_run"):
            failed_code = str(result.get("error_code") or result.get("summary") or "SCRIPT_EXECUTION_FAILED")
            lifecycle = "BLOCKED_DEPLOYMENT" if failed_code in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE"} else "PARTIAL" if result.get("status") == "PARTIAL" else "FAILED"
            await self._set_script_record_status(session, run, script_record, lifecycle, execution_error=None if lifecycle == "PARTIAL" else failed_code)
        unified = self._unified_result(result, artifact, permitted_tools)
        if name == "mysql_metadata_discovery":
            await solver_state_service.record_metadata_progress(
                session,
                run,
                stage=str(arguments.get("stage") or result.get("stage") or "").lower(),
                result_status=unified.status,
                error_code=unified.error_code,
                diagnostic=(result.get("diagnostic") if isinstance(result.get("diagnostic"), dict) else {}),
            )
        if approved_action_id:
            approved = await session.get(ApprovedAction, approved_action_id)
            if approved is not None:
                if unified.status in _TOOL_SUCCESS_STATUSES and int(approved.used_logical_calls or 0) >= int(approved.max_logical_calls or 1):
                    approved.status = "CONSUMED"
                elif unified.status not in _TOOL_SUCCESS_STATUSES:
                    approved.status = "REJECTED"
                await session.flush()
        facts = self._facts(name, result, relative.replace("\\", "/"))
        facts["tool_model_view"] = unified.model_view.model_dump()
        observation = Observation(
            run_id=run.id,
            tool_call_id=call.id,
            artifact_id=artifact.id if artifact else None,
            observation_type="tool_result",
            summary=str(result.get("summary", "Tool execution completed"))[:1000],
            facts_json=facts,
        )
        session.add(observation)
        await session.commit()
        logical.result_observation_id = observation.id
        try:
            await effective_logical_tool_call_service.trace(
                session,
                logical,
                execution_layer=execution_layer,
                event_type="completed" if unified.status in _TOOL_SUCCESS_STATUSES else "failed",
                external_id=call.runner_job_id,
                payload=result,
            )
            await session.commit()
            if unified.status in _TOOL_SUCCESS_STATUSES:
                clear_failure(run)
                await session.commit()
        except Exception as error:
            await session.rollback()
            result = {
                **result,
                "status": "FAILED",
                "error_code": "BACKEND_PERSISTENCE_FAILED",
                "stage": "TRACE_WRITE",
                "tool_execution_completed": _result_contract_status(result) in _TOOL_EXECUTION_COMPLETED_STATUSES,
                "summary": "Tool completed but trace persistence failed",
                "error": str(error),
            }
            record_failure(run, code="BACKEND_PERSISTENCE_FAILED", message=str(error), stage="TRACE_WRITE")
            await session.commit()
            unified = self._unified_result(result, artifact, permitted_tools)
        if artifact_event_payload:
            await event_service.append(session, run.id, "artifact.created", artifact_event_payload)
        candidates = []
        if artifact is not None and target.is_file():
            candidates = await flag_service.extract_candidates(
                session, run, challenge, artifact, target.read_text(encoding="utf-8", errors="replace")
            )
        observation.facts_json["flag_candidate_count"] = len(candidates)
        await session.commit()
        if name == "file_read":
            structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
            if structured.get("path") and structured.get("content_sha256"):
                await solver_state_service.record_file_read(
                    session,
                    run.id,
                    path=str(structured.get("path")),
                    start_line=int(structured.get("start_line") or arguments.get("start_line") or 1),
                    end_line=int(structured.get("end_line") or arguments.get("end_line") or 1),
                    content_sha256=str(structured.get("content_sha256")),
                )
        event_type = "tool.completed" if unified.status in _TOOL_SUCCESS_STATUSES else "tool.failed"
        await event_service.append(
            session, run.id, event_type, {"tool_call_id": call.id, "logical_tool_call_id": call.logical_tool_call_id, "tool": name, "execution_layer": call.execution_layer, "result": unified.model_dump()}
        )
        # Enqueue only after the result is durably returned to the caller. The
        # worker owns the lease and any compaction failure is isolated from
        # this tool delivery path.
        compaction_scheduler.enqueue(run.id)
        return unified.model_dump()

    async def _cached_file_read(
        self, session: AsyncSession, run: SolveRun, arguments: dict
    ) -> dict | None:
        """Return the original bounded view for an identical file range.

        The Runner is not contacted twice for content the model has already
        received.  Keeping this lookup in the gateway gives both provider
        engines identical behavior.
        """
        calls = list(
            (
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run.id, ToolCall.tool_name == "file_read")
                    .order_by(ToolCall.created_at.desc())
                )
            ).all()
        )
        for call in calls:
            if dict(call.arguments_json or {}) != arguments:
                continue
            observation = await session.scalar(
                select(Observation)
                .where(Observation.tool_call_id == call.id)
                .order_by(Observation.created_at.desc())
            )
            if not observation:
                continue
            view = (observation.facts_json or {}).get("tool_model_view")
            if not isinstance(view, dict) or not view.get("content_excerpt"):
                continue
            return ToolExecutionResult(
                status="CACHED",
                model_view=ToolModelView(
                    summary=str(view.get("summary") or "已返回此前读取的文件范围"),
                    content_excerpt=str(view.get("content_excerpt")),
                    extracted_facts=dict(view.get("extracted_facts") or {}),
                    warnings=[*list(view.get("warnings") or []), "FILE_RANGE_ALREADY_AVAILABLE"],
                    suggested_next_dimensions=list(view.get("suggested_next_dimensions") or []),
                ),
                artifacts=[],
                error_code=None,
                error_message="The same file range was already returned to the model; Runner was not called.",
                warning="FILE_RANGE_ALREADY_AVAILABLE",
                content=str(view.get("content_excerpt")),
                summary=str(view.get("summary") or "Cached file range returned"),
                required_next_dimension="automation_or_new_experiment",
                stage="CACHE",
                tool_execution_completed=True,
            )
        return None

    @staticmethod
    def _redact(text: str | None) -> str | None:
        if text is None:
            return None
        import re

        value = text[:8192]
        value = re.sub(r"(?i)(authorization|cookie|set-cookie|token|api[_-]?key|password)(\s*[:=]\s*)([^;\s,]+)", r"\1\2<redacted>", value)
        return value

    def _unified_result(
        self, result: dict, artifact: Artifact | None, permitted_tools: set[str]
    ) -> ToolExecutionResult:
        status = _result_contract_status(result)
        if status not in _TOOL_SUCCESS_STATUSES | {"NO_FACT", "CONTRACT_ERROR", "FAILED", "TIMEOUT", "CANCELLED"}:
            status = "FAILED"
        structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
        facts = dict(structured.get("extracted_facts") or result.get("extracted_facts") or result.get("facts") or {})
        for key in ("status_code", "final_url", "redirect_history", "content_type", "selected_headers", "cookie_names", "body_length", "html_title", "html_comments", "forms", "form_actions", "parameter_names", "links", "script_urls", "json_keys", "suspected_credentials", "suspected_flags", "path", "start_line", "end_line", "content_sha256", "matching_paths", "match_snippets", "line_numbers", "generated_files", "stdout_excerpt", "stderr_excerpt", "network_targets", "runtime_ms", "injectable", "parameter", "technique", "dbms", "databases", "tables", "columns", "dumped_rows", "flag_candidates", "raw_output_path", "sqlmap_extraction_completed"):
            if key in structured and key not in facts:
                facts[key] = structured[key]
        excerpt = structured.get("body_excerpt") or structured.get("content_excerpt") or structured.get("content") or structured.get("output")
        if excerpt is None and structured.get("match_snippets") is not None:
            excerpt = json.dumps(structured.get("match_snippets"), ensure_ascii=False)
        warnings = []
        if structured.get("truncated"):
            warnings.append("结果正文已截断，完整内容保存在 Artifact")
        if status not in _TOOL_SUCCESS_STATUSES:
            warnings.append("工具执行未成功完成")
        suggestions = []
        diagnostic = result.get("diagnostic") if isinstance(result.get("diagnostic"), dict) else {}
        if facts.get("status_code") in {301, 302, 303, 307, 308}:
            suggestions.append("检查重定向目标和登录流程")
        if facts.get("suspected_credentials"):
            suggestions.append("核对凭据线索并进行最小化登录验证")
        return ToolExecutionResult(
            status=status,
            model_view=ToolModelView(
                summary=str(structured.get("summary") or result.get("summary") or "工具执行完成")[:1000],
                content_excerpt=self._redact(str(excerpt) if excerpt is not None else None),
                extracted_facts=facts,
                warnings=warnings,
                suggested_next_dimensions=suggestions,
            ),
            artifacts=[ToolArtifactRef(artifact_id=artifact.id, relative_path=artifact.file_path, sha256=artifact.sha256, size=artifact.size, mime_type=artifact.mime_type or "text/plain")] if artifact else [],
            error_code=str(result.get("error_code") or ("MYSQL_METADATA_EMPTY_RESULT" if status == "NO_FACT" else "MYSQL_METADATA_CONTRACT_ERROR" if status == "CONTRACT_ERROR" else "RUNNER_ERROR")) if status not in _TOOL_SUCCESS_STATUSES else None,
            error_message=str(result.get("error") or result.get("error_message") or diagnostic.get("reason") or "") if status not in _TOOL_SUCCESS_STATUSES else None,
            retryable=bool(result.get("retryable", status in {"FAILED", "TIMEOUT", "NO_FACT"})),
            stage=str(result.get("stage") or "EXECUTION"),
            diagnostic_id=str(result.get("diagnostic_id")) if result.get("diagnostic_id") else None,
            tool_execution_completed=bool(result.get("tool_execution_completed", status in _TOOL_EXECUTION_COMPLETED_STATUSES)),
            error_details={
                "reason": str(result.get("error") or result.get("error_message") or diagnostic.get("reason") or result.get("summary") or ""),
                "available_tools": sorted(permitted_tools),
                "readable_workspace": ["challenge.json", "AGENTS.md", "source/**", "attachments/**", "requests/**", "responses/**", "outputs/**", "evidence/**", "scripts/**", "notes/**", "final/**", "scratch/**"],
                "recommended_action": "Fix the bounded arguments or choose a different minimal experiment; the run may continue.",
                "auto_retry": status in {"TIMEOUT", "NO_FACT"},
                "contract_status": status,
            } if status not in _TOOL_SUCCESS_STATUSES else {},
        )

    @staticmethod
    def _facts(name: str, result: dict, artifact_path: str) -> dict:
        base = {
            "tool": name,
            "ok": _result_contract_status(result) in _TOOL_SUCCESS_STATUSES,
            "artifact_path": artifact_path,
        }
        structured = result.get("structured_result", result)
        if name == "http_request":
            return {
                **base,
                "status_code": structured.get("status_code"),
                "content_type": structured.get("headers", {}).get("content-type"),
                "body_length": len(str(structured.get("body", ""))),
                "redirect_count": structured.get("redirect_count", 0),
                "final_url": structured.get("final_url"),
            }
        if name in {"file_read", "file_search"}:
            return {
                **base,
                "path": structured.get("path"),
                "size": structured.get("size"),
                "truncated": structured.get("truncated", False),
                "matching_paths": structured.get("matching_paths", []),
                "match_snippets": structured.get("match_snippets", []),
                "line_numbers": structured.get("line_numbers", []),
            }
        return {
            **base,
            "exit_code": structured.get("exit_code"),
            "output_length": len(str(structured.get("output", ""))),
            "truncated": structured.get("truncated", False),
        }


tool_gateway = ToolGateway()
