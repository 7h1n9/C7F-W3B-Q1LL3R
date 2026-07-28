"""Durable queued Runner jobs with bounded concurrency and cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app.executors.kali_vm import KaliVmExecutionBackend
from app.models import Job, JobRequest, JobStatus
from app.workspace.paths import initialize_workspace, workspace_for


class JobService:
    GLOBAL_CONCURRENCY = 4
    PER_RUN_CONCURRENCY = 2

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.backend = KaliVmExecutionBackend()
        self.global_slots = asyncio.Semaphore(self.GLOBAL_CONCURRENCY)
        self.run_slots: dict[str, asyncio.Semaphore] = {}
        self.sqlmap_slots = asyncio.Semaphore(1)
        self.script_slots = asyncio.Semaphore(1)
        self._lock = asyncio.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _job_path(self, run_id: str, job_id: str) -> Path:
        return workspace_for(run_id) / ".jobs" / f"{job_id}.json"

    def _save(self, job: Job) -> None:
        path = self._job_path(job.request.run_id, job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = job.model_dump(mode="json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    async def recover(self) -> None:
        root = workspace_for("placeholder").parent
        if not root.exists():
            return
        for path in root.glob("*/.jobs/*.json"):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.FAILED
                job.error = "RUNNER_RESTARTED"
                job.result = self._result_payload({"status": "FAILED", "error_code": "RUNNER_RESTARTED", "summary": "Runner restarted while the job was running", "stage": "RECOVERY"})
                job.finished_at = self._now()
                self._save(job)
            self.jobs[job.job_id] = job

    async def create(self, request: JobRequest) -> Job:
        initialize_workspace(request.run_id)
        job = Job(job_id=str(uuid.uuid4()), request=request, created_at=self._now())
        self.jobs[job.job_id] = job
        self._save(job)
        self.tasks[job.job_id] = asyncio.create_task(self._run(job))
        self._refresh_queue_positions()
        return job

    def _refresh_queue_positions(self) -> None:
        queued = sorted((job for job in self.jobs.values() if job.status == JobStatus.QUEUED), key=lambda item: item.created_at or "")
        for index, job in enumerate(queued, 1):
            job.queue_position = index
            self._save(job)

    @staticmethod
    def _category(tool: str) -> str | None:
        if tool == "sqlmap_run" or tool == "sqlmap_detect":
            return "sqlmap"
        if tool in {"script_run", "python_run", "sandbox_exec"}:
            return "script"
        return None

    async def _run(self, job: Job) -> None:
        run_slot = self.run_slots.setdefault(job.request.run_id, asyncio.Semaphore(self.PER_RUN_CONCURRENCY))
        category = self._category(job.request.tool)
        category_slot = self.sqlmap_slots if category == "sqlmap" else self.script_slots if category == "script" else None
        try:
            async with self.global_slots:
                async with run_slot:
                    if category_slot is None:
                        await self._execute(job)
                    else:
                        async with category_slot:
                            await self._execute(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.error = "RUNNER_JOB_CANCELLED"
            job.result = self._result_payload({"status": "CANCELLED", "error_code": "RUNNER_JOB_CANCELLED", "summary": "Runner job cancelled", "stage": "CANCELLATION"})
            job.finished_at = self._now()
            self._save(job)
            raise
        finally:
            self._refresh_queue_positions()

    @staticmethod
    def _result_payload(result: dict) -> dict:
        status = str(result.get("status") or "COMPLETED").upper()
        if status not in {item.value for item in JobStatus}:
            status = "FAILED"
        return {
            **result,
            "status": status,
            "error_code": result.get("error_code"),
            "diagnostic_id": result.get("diagnostic_id") or (str(uuid.uuid4()) if status == "FAILED" else None),
            "tool_execution_completed": bool(result.get("tool_execution_completed", status == "COMPLETED")),
            "retryable": bool(result.get("retryable", status in {"FAILED"})),
            "stage": str(result.get("stage") or "EXECUTION"),
            "summary": str(result.get("summary") or ""),
            "structured_result": result.get("structured_result") if isinstance(result.get("structured_result"), dict) else result,
            "artifact_paths": list(result.get("artifact_paths") or ([result["artifact_path"]] if result.get("artifact_path") else [])),
        }

    async def _execute(self, job: Job) -> None:
        job.status, job.started_at, job.queue_position = JobStatus.RUNNING, self._now(), None
        self._save(job)
        try:
            request = job.request.model_copy(update={"arguments": {**job.request.arguments, "job_id": job.job_id}})
            result = await self.backend.execute(request)
            normalized = self._result_payload(result)
            exit_code = normalized.get("exit_code")
            if exit_code not in (None, 0) and normalized["status"] == "COMPLETED":
                normalized.update(status="FAILED", error_code=normalized.get("error_code") or "RUNNER_NONZERO_EXIT", retryable=False, stage="EXECUTION")
            if job.request.tool == "script_run" and normalized["status"] == "COMPLETED":
                expected = workspace_for(job.request.run_id) / "outputs" / "scripts" / job.job_id / "result.json"
                if not expected.is_file():
                    normalized.update(status="FAILED", error_code="SCRIPT_RESULT_MISSING", retryable=False, stage="RESULT_VALIDATION", summary="Script did not produce result.json")
                else:
                    try:
                        parsed = json.loads(expected.read_text(encoding="utf-8"))
                        if not isinstance(parsed, dict):
                            raise ValueError("result.json must contain an object")
                        if str(parsed.get("status") or "") not in {"COMPLETED", "PARTIAL"}:
                            raise ValueError("result.json status must be COMPLETED or PARTIAL")
                        normalized["status"] = str(parsed["status"])
                        structured = parsed.get("structured_result")
                        if not isinstance(structured, dict):
                            raise ValueError("result.json structured_result must be an object")
                        normalized["structured_result"] = structured
                        normalized["script_result"] = parsed
                        normalized["result_json_path"] = str(expected.relative_to(workspace_for(job.request.run_id))).replace("\\", "/")
                        normalized["artifact_paths"] = sorted(set([*(normalized.get("artifact_paths") or []), normalized["result_json_path"]]))
                    except (OSError, ValueError, json.JSONDecodeError) as error:
                        normalized.update(status="FAILED", error_code="SCRIPT_RESULT_INVALID", retryable=False, stage="RESULT_VALIDATION", summary=f"Script result.json is invalid: {error}")
            self._persist_standard_script_artifacts(job, normalized)
            job.result = self._persist_artifact(job, normalized)
            job.status = JobStatus(normalized["status"])
            job.error = normalized.get("error") or normalized.get("error_message")
        except asyncio.CancelledError:
            raise
        except HTTPException as error:
            detail = str(error.detail)
            code = detail.split(":", 1)[0] if detail and detail.split(":", 1)[0].isupper() else "RUNNER_JOB_FAILED"
            job.status = JobStatus.FAILED
            job.error = detail
            job.result = self._persist_artifact(job, self._result_payload({"status": "FAILED", "error_code": code, "summary": detail, "error": detail, "stage": "VALIDATION" if error.status_code < 500 else "EXECUTION"}))
        except Exception as error:
            job.status = JobStatus.FAILED
            job.error = str(error)
            job.result = self._persist_artifact(job, {"status": "FAILED", "error_code": "RUNNER_JOB_FAILED", "summary": "Runner execution failed", "error": str(error), "stage": "EXECUTION"})
        finally:
            job.finished_at = self._now()
            self._save(job)

    def _persist_artifact(self, job: Job, result: dict) -> dict:
        workspace = workspace_for(job.request.run_id)
        directory = "responses" if job.request.tool == "http_request" else "outputs"
        suffix = "json" if job.request.tool in {"http_request", "file_search", "pcap_metadata", "pcap_protocols", "pcap_query", "sqlmap_run", "sqlmap_detect"} else "txt"
        path = workspace / directory / f"{job.job_id}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(result, ensure_ascii=False, indent=2).encode() if suffix == "json" else str(result.get("output") or result.get("content") or json.dumps(result, ensure_ascii=False, indent=2)).encode()
        path.write_bytes(raw)
        return {**result, "artifact_path": str(path.relative_to(workspace)).replace("\\", "/"), "artifact_size": len(raw), "artifact_sha256": hashlib.sha256(raw).hexdigest(), "artifact_paths": sorted(set([*(result.get("artifact_paths") or []), str(path.relative_to(workspace)).replace("\\", "/")])), "structured_result": result}

    def _persist_standard_script_artifacts(self, job: Job, result: dict) -> None:
        if job.request.tool not in {"script_run", "python_run"}:
            return
        workspace = workspace_for(job.request.run_id)
        output_dir = workspace / "outputs" / "scripts" / job.job_id
        evidence_dir = workspace / "evidence" / "scripts" / job.job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        progress = output_dir / "progress.jsonl"
        checkpoint = output_dir / "checkpoint.json"
        progress.write_text(json.dumps({"at": self._now(), "status": result.get("status") if result.get("status") in {"COMPLETED", "PARTIAL"} else "FAILED", "stage": result.get("stage")}, ensure_ascii=False) + "\n", encoding="utf-8")
        checkpoint.write_text(json.dumps({"job_id": job.job_id, "status": result.get("status"), "error_code": result.get("error_code")}, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence_dir / "request-summary.json").write_text(json.dumps({"job_id": job.job_id, "run_id": job.request.run_id, "tool": job.request.tool, "path": job.request.arguments.get("path"), "interpreter": job.request.arguments.get("interpreter"), "network_mode": job.request.arguments.get("network_mode", "none"), "allowed_hosts": job.request.allowed_hosts}, ensure_ascii=False, indent=2), encoding="utf-8")
        standard = [str(path.relative_to(workspace)).replace("\\", "/") for path in (progress, checkpoint, evidence_dir / "request-summary.json")]
        result_file = output_dir / "result.json"
        if result_file.is_file():
            standard.append(str(result_file.relative_to(workspace)).replace("\\", "/"))
        result["artifact_paths"] = sorted(set([*(result.get("artifact_paths") or []), *standard]))
        result["progress_path"] = standard[0]
        result["checkpoint_path"] = standard[1]

    async def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise HTTPException(404, detail="job not found")
        return self.jobs[job_id]

    async def cancel(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await task
        with __import__("contextlib").suppress(Exception):
            await self.backend.cancel(job_id)
        job.status, job.finished_at = JobStatus.CANCELLED, self._now()
        job.result = self._result_payload({"status": "CANCELLED", "error_code": "RUNNER_JOB_CANCELLED", "summary": "Runner job cancelled", "stage": "CANCELLATION"})
        self._save(job)
        return job


job_service = JobService()
