import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus
from app.schemas.tool import ToolArtifactRef, ToolExecutionResult, ToolModelView
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.runner_client import runner_client
from app.services.tool_permissions import effective_tools_for
from app.tools.policy import enforce_tool_policy
from app.tools.registry import load_tool_definitions


class ToolGateway:
    async def invoke(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, name: str, arguments: dict
    ) -> dict:
        definition = load_tool_definitions().get(name)
        if not definition or not definition.enabled:
            raise DomainError(
                "TOOL_NOT_AVAILABLE", "Requested tool is not enabled.", {"tool": name}, 404
            )
        if run.status != RunStatus.EXECUTING:
            raise DomainError(
                "RUN_TOOL_NOT_ALLOWED",
                "Tools can only execute while the run is in EXECUTING state.",
                {"current_state": run.status},
                409,
            )
        if name not in await effective_tools_for(session, run, challenge):
            raise DomainError(
                "TOOL_NOT_ALLOWED_FOR_CHALLENGE",
                "This tool is not allowed by the current role or challenge limits.",
                {"tool": name, "challenge_type": challenge.challenge_type},
                422,
            )
        arguments = definition.validate_arguments(arguments)
        enforce_tool_policy(name, arguments, challenge.allowed_hosts)
        if name == "file_read":
            cached = await self._cached_file_read(session, run, arguments)
            if cached is not None:
                await event_service.append(
                    session,
                    run.id,
                    "tool.read_deduplicated",
                    {"tool": name, "code": "FILE_RANGE_ALREADY_READ", "path": arguments.get("path")},
                )
                return cached.model_dump()
        call = ToolCall(
            run_id=run.id,
            tool_name=name,
            arguments_json=arguments,
            status="REQUESTED",
            started_at=datetime.now(UTC),
        )
        session.add(call)
        await session.commit()
        await session.refresh(call)
        await event_service.append(
            session, run.id, "tool.requested", {"tool_call_id": call.id, "tool": name}
        )
        call.status = "STARTED"
        await session.commit()
        await event_service.append(
            session, run.id, "tool.started", {"tool_call_id": call.id, "tool": name}
        )
        try:
            # ctfctl can create a bounded scripts/*.py file during a turn.
            # The remote Runner has its own per-run workspace, so synchronize
            # that file immediately before python_run instead of treating a
            # missing remote copy as a request for arbitrary shell access.
            if name == "python_run":
                await runner_client.sync_workspace(run.id, Path(run.workspace_path))
            job_id = await runner_client.create_job(
                run.id, challenge.allowed_hosts, name, arguments
            )
            result = await runner_client.wait_job(job_id)
            if (
                name == "file_read"
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
                result = await runner_client.wait_job(retry_job_id)
                if result.get("status") != "COMPLETED":
                    result["error_code"] = "FILE_NOT_FOUND"
        except Exception as error:
            result = {"status": "FAILED", "summary": "Runner request failed", "error": str(error)}
        call.status, call.runner_job_id, call.finished_at = (
            ("COMPLETED" if result.get("status") == "COMPLETED" else "FAILED"),
            result.get("job_id"),
            datetime.now(UTC),
        )
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
                    "summary": "Artifact download failed",
                    "error": str(error),
                }
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
        unified = self._unified_result(result, artifact)
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
        if artifact_event_payload:
            await event_service.append(session, run.id, "artifact.created", artifact_event_payload)
        candidates = []
        if artifact is not None and target.is_file():
            candidates = await flag_service.extract_candidates(
                session, run, challenge, artifact, target.read_text(encoding="utf-8", errors="replace")
            )
        observation.facts_json["flag_candidate_count"] = len(candidates)
        await session.commit()
        event_type = "tool.completed" if unified.status == "COMPLETED" else "tool.failed"
        await event_service.append(
            session, run.id, event_type, {"tool_call_id": call.id, "result": unified.model_dump()}
        )
        return unified.model_dump()

    async def _cached_file_read(
        self, session: AsyncSession, run: SolveRun, arguments: dict
    ) -> ToolExecutionResult | None:
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
                status="COMPLETED",
                model_view=ToolModelView(
                    summary=str(view.get("summary") or "已返回此前读取的文件范围"),
                    content_excerpt=str(view.get("content_excerpt")),
                    extracted_facts=dict(view.get("extracted_facts") or {}),
                    warnings=[*list(view.get("warnings") or []), "FILE_RANGE_ALREADY_READ"],
                    suggested_next_dimensions=list(view.get("suggested_next_dimensions") or []),
                ),
                artifacts=[],
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

    def _unified_result(self, result: dict, artifact: Artifact | None) -> ToolExecutionResult:
        status = str(result.get("status") or "FAILED")
        if status not in {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}:
            status = "FAILED"
        structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result
        facts = dict(structured.get("extracted_facts") or result.get("extracted_facts") or {})
        for key in ("status_code", "final_url", "redirect_history", "content_type", "selected_headers", "cookie_names", "body_length", "html_title", "html_comments", "forms", "form_actions", "parameter_names", "links", "script_urls", "json_keys", "suspected_credentials", "suspected_flags", "path", "start_line", "end_line", "content_sha256"):
            if key in structured and key not in facts:
                facts[key] = structured[key]
        excerpt = structured.get("body_excerpt") or structured.get("content_excerpt") or structured.get("content") or structured.get("output")
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
            }
        return {
            **base,
            "exit_code": structured.get("exit_code"),
            "output_length": len(str(structured.get("output", ""))),
            "truncated": structured.get("truncated", False),
        }


tool_gateway = ToolGateway()
