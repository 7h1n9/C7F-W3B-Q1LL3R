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
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.backend = KaliVmExecutionBackend()

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
                job.status, job.error, job.finished_at = JobStatus.FAILED, "RUNNER_RESTARTED", self._now()
                self._save(job)
            self.jobs[job.job_id] = job

    async def create(self, request: JobRequest) -> Job:
        initialize_workspace(request.run_id)
        job = Job(job_id=str(uuid.uuid4()), request=request, created_at=self._now())
        self.jobs[job.job_id] = job
        self._save(job)
        self.tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        job.status, job.started_at = JobStatus.RUNNING, self._now()
        self._save(job)
        try:
            result = await self.backend.execute(job.request)
            job.result = self._persist_artifact(job, result)
            job.status = JobStatus.COMPLETED
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.result = self._persist_artifact(job, {"summary": "Runner job cancelled"})
            raise
        except Exception as error:
            job.status, job.error = JobStatus.FAILED, str(error)
            job.result = self._persist_artifact(job, {"summary": "Runner execution failed", "error": str(error)})
        finally:
            job.finished_at = self._now()
            self._save(job)

    def _persist_artifact(self, job: Job, result: dict) -> dict:
        workspace = workspace_for(job.request.run_id)
        directory = "responses" if job.request.tool == "http_request" else "outputs"
        suffix = "json" if job.request.tool in {"http_request", "file_search", "pcap_metadata", "pcap_protocols", "pcap_query"} else "txt"
        path = workspace / directory / f"{job.job_id}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(result, ensure_ascii=False, indent=2).encode() if suffix == "json" else str(result.get("output") or result.get("content") or json.dumps(result, ensure_ascii=False, indent=2)).encode()
        path.write_bytes(raw)
        return {**result, "artifact_path": str(path.relative_to(workspace)).replace("\\", "/"), "artifact_size": len(raw), "artifact_sha256": hashlib.sha256(raw).hexdigest(), "structured_result": result}

    async def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise HTTPException(404, detail="job not found")
        return self.jobs[job_id]

    async def cancel(self, job_id: str) -> Job:
        job = await self.get(job_id)
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        job.status, job.finished_at = JobStatus.CANCELLED, self._now()
        self._save(job)
        return job


job_service = JobService()
