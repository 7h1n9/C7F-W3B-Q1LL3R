import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.orchestration.state_machine import RunStatus
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
            job_id = await runner_client.create_job(
                run.id, challenge.allowed_hosts, name, arguments
            )
            result = await runner_client.wait_job(job_id)
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
        if not relative:
            relative = f"outputs/runner_error_{call.id}.json"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            size, checksum = target.stat().st_size, hashlib.sha256(target.read_bytes()).hexdigest()
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
        facts = self._facts(name, result, artifact.file_path)
        observation = Observation(
            run_id=run.id,
            tool_call_id=call.id,
            artifact_id=artifact.id,
            observation_type="tool_result",
            summary=artifact.summary,
            facts_json=facts,
        )
        session.add(observation)
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "artifact.created",
            {
                "artifact_id": artifact.id,
                "path": artifact.file_path,
                "size": artifact.size,
                "sha256": artifact.sha256,
            },
        )
        candidates = await flag_service.extract_candidates(
            session, run, challenge, artifact, target.read_text(encoding="utf-8", errors="replace")
        )
        observation.facts_json["flag_candidate_count"] = len(candidates)
        await session.commit()
        event_type = "tool.completed" if result.get("status") == "COMPLETED" else "tool.failed"
        await event_service.append(
            session, run.id, event_type, {"tool_call_id": call.id, "result": result}
        )
        return result

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
