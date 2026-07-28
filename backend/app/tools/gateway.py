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
from app.models.run import (
    AgentTurn,
    Artifact,
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
from app.services.tool_argument_adapter import adapt_arguments
from app.services.tool_invocation_coordinator import tool_invocation_coordinator
from app.services.tool_permissions import effective_tools_for
from app.services.workspace_sync import workspace_sync_service
from app.tools.policy import enforce_tool_policy
from app.tools.registry import load_tool_definitions

_SECRET_KEYS = {"token", "password", "passwd", "secret", "api_key", "authorization", "cookie", "set-cookie"}


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
    async def invoke(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, name: str, arguments: dict,
        *, logical_tool_call_id: str | None = None, parent_tool_call_id: str | None = None,
        execution_layer: str = "gateway", turn_id: str | None = None,
        provider_tool_name: str | None = None, logical_kind: str = "TOOL",
        required_action: bool = False,
        required_action_kind: str | None = None,
    ) -> dict:
        definition = load_tool_definitions().get(name)
        if not definition or not definition.enabled:
            raise DomainError(
                "TOOL_NOT_AVAILABLE", "Requested tool is not enabled.", {"tool": name}, 404
            )
        # A model turn can arrive while the Run is moving through one of the
        # short-lived execution stages.  Only terminal/explicit pause states
        # are rejected here; attempt/lease freshness is checked in one place.
        coordinated = await tool_invocation_coordinator.validate(session, run)
        lease = coordinated["lease"]
        permitted_tools = await effective_tools_for(session, run, challenge)
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
        arguments = adapt_arguments(name, arguments, challenge)
        try:
            arguments = definition.validate_arguments(arguments)
        except DomainError as error:
            if error.code == "TOOL_INVALID_ARGUMENT":
                details = dict(error.details or {})
                details.update({"missing_fields": details.get("errors", []), "unknown_fields": [], "expected_schema": definition.parameters, "corrected_example": adapt_arguments(name, arguments), "available_operations": [name]})
                raise DomainError(error.code, error.message, details, error.status_code) from error
            raise
        enforce_tool_policy(name, arguments, challenge.allowed_hosts)
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
        call = ToolCall(
            run_id=run.id,
            tool_name=name,
            arguments_json=_redact_arguments(arguments),
            status="REQUESTED",
            started_at=datetime.now(UTC),
            logical_tool_call_id=logical_tool_call_id or str(uuid.uuid4()),
            parent_tool_call_id=parent_tool_call_id,
            execution_layer=execution_layer,
            counts_toward_budget=True,
            logical_kind=logical_kind,
            provider_tool_name=provider_tool_name or name,
            effective_tool_name=name,
            turn_id=turn_id,
        )
        session.add(call)
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
            with contextlib.suppress(Exception):
                await workspace_sync_service.sync_from_runner(run.id, Path(run.workspace_path))
            if result.get("status") != "COMPLETED" and not result.get("error_code"):
                error_text = str(result.get("error") or result.get("summary") or "").lower()
                if "not found" in error_text or "does not exist" in error_text:
                    result["error_code"] = "FILE_NOT_FOUND"
                elif "not installed" in error_text:
                    result["error_code"] = "TOOL_NOT_INSTALLED"
                elif "script" in error_text and "sync" in error_text:
                    result["error_code"] = "SCRIPT_NOT_SYNCED"
            if (
                name in {"file_read", "python_run", "script_run"}
                and result.get("status") != "COMPLETED"
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
                if result.get("status") != "COMPLETED" and name == "file_read":
                    result["error_code"] = "FILE_NOT_FOUND"
            if result.get("error_code") in {"TARGET_UNAVAILABLE", "BACKEND_UNAVAILABLE", "RUNNER_UNAVAILABLE", "TOOL_RESULT_DELIVERY_FAILED"}:
                record_failure(run, code=str(result["error_code"]), message=str(result.get("error") or result.get("summary") or result["error_code"]), stage=str(result.get("stage") or "EXECUTION"))
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
        call.status, call.runner_job_id, call.finished_at = (
            ("COMPLETED" if result.get("status") == "COMPLETED" else "FAILED"),
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
                    "tool_execution_completed": result.get("status") == "COMPLETED",
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
        if name in {"script_run", "python_run"} and artifact is not None:
            provenance = arguments.get("assumption_provenance") or []
            level = assistance_level(provenance)
            existing_script = await session.scalar(
                select(ScriptRecord).where(
                    ScriptRecord.run_id == run.id,
                    ScriptRecord.path == str(arguments.get("path") or artifact.file_path),
                ).order_by(ScriptRecord.created_at.desc())
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
                    status="COMPLETED" if result.get("status") == "COMPLETED" else "FAILED",
                    tool_call_id=call.id,
                    result_artifact_id=artifact.id,
                )
                session.add(existing_script)
            else:
                existing_script.artifact_id = artifact.id
                existing_script.status = "COMPLETED" if result.get("status") == "COMPLETED" else "PARTIAL" if result.get("status") == "PARTIAL" else "FAILED"
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
        unified = self._unified_result(result, artifact, permitted_tools)
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
                event_type="completed" if unified.status == "COMPLETED" else "failed",
                external_id=call.runner_job_id,
                payload=result,
            )
            await session.commit()
            if unified.status == "COMPLETED":
                clear_failure(run)
                await session.commit()
        except Exception as error:
            await session.rollback()
            result = {
                **result,
                "status": "FAILED",
                "error_code": "BACKEND_PERSISTENCE_FAILED",
                "stage": "TRACE_WRITE",
                "tool_execution_completed": result.get("status") == "COMPLETED",
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
        event_type = "tool.completed" if unified.status == "COMPLETED" else "tool.failed"
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
        status = str(result.get("status") or "FAILED")
        if status not in {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}:
            status = "FAILED"
        structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
        facts = dict(structured.get("extracted_facts") or result.get("extracted_facts") or {})
        for key in ("status_code", "final_url", "redirect_history", "content_type", "selected_headers", "cookie_names", "body_length", "html_title", "html_comments", "forms", "form_actions", "parameter_names", "links", "script_urls", "json_keys", "suspected_credentials", "suspected_flags", "path", "start_line", "end_line", "content_sha256", "matching_paths", "match_snippets", "line_numbers", "generated_files", "stdout_excerpt", "stderr_excerpt", "network_targets", "runtime_ms", "injectable", "parameter", "technique", "dbms", "databases", "tables", "columns", "dumped_rows", "flag_candidates", "raw_output_path", "sqlmap_extraction_completed"):
            if key in structured and key not in facts:
                facts[key] = structured[key]
        excerpt = structured.get("body_excerpt") or structured.get("content_excerpt") or structured.get("content") or structured.get("output")
        if excerpt is None and structured.get("match_snippets") is not None:
            excerpt = json.dumps(structured.get("match_snippets"), ensure_ascii=False)
        warnings = []
        if structured.get("truncated"):
            warnings.append("结果正文已截断，完整内容保存在 Artifact")
        if status != "COMPLETED":
            warnings.append("工具执行未成功完成")
        suggestions = []
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
            error_code=str(result.get("error_code") or "RUNNER_ERROR") if status != "COMPLETED" else None,
            error_message=str(result.get("error") or result.get("error_message")) if status != "COMPLETED" else None,
            retryable=status in {"FAILED", "TIMEOUT"},
            stage=str(result.get("stage") or "EXECUTION"),
            diagnostic_id=str(result.get("diagnostic_id")) if result.get("diagnostic_id") else None,
            tool_execution_completed=bool(result.get("tool_execution_completed", status == "COMPLETED")),
            error_details={
                "reason": str(result.get("error") or result.get("error_message") or result.get("summary") or ""),
                "available_tools": sorted(permitted_tools),
                "readable_workspace": ["challenge.json", "AGENTS.md", "source/**", "attachments/**", "requests/**", "responses/**", "outputs/**", "evidence/**", "scripts/**", "notes/**", "final/**", "scratch/**"],
                "recommended_action": "Fix the bounded arguments or choose a different minimal experiment; the run may continue.",
                "auto_retry": status in {"TIMEOUT"},
            } if status != "COMPLETED" else {},
        )

    @staticmethod
    def _facts(name: str, result: dict, artifact_path: str) -> dict:
        base = {
            "tool": name,
            "ok": result.get("status") == "COMPLETED",
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
