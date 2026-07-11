import asyncio
import hashlib
import json
import uuid

from fastapi import HTTPException

from app.executors.kali_vm import KaliVmExecutionBackend
from app.models import Job, JobRequest, JobStatus
from app.workspace.paths import workspace_for


class JobService:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.backend = KaliVmExecutionBackend()

    async def create(self, request: JobRequest) -> Job:
        job = Job(job_id=str(uuid.uuid4()), request=request)
        self.jobs[job.job_id] = job
        self.tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
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

    def _persist_artifact(self, job: Job, result: dict) -> dict:
        workspace = workspace_for(job.request.run_id, job.request.workspace_path)
        directory = "responses" if job.request.tool == "http_request" else "outputs"
        suffix = "json" if job.request.tool in {"http_request", "file_search"} else "txt"
        folder = workspace / directory
        folder.mkdir(parents=True, exist_ok=True)
        number = len(list(folder.glob(f"{job.request.tool}_*.{suffix}"))) + 1
        path = folder / f"{job.request.tool}_{number:04d}.{suffix}"
        if suffix == "json":
            raw = json.dumps(result, ensure_ascii=False, indent=2).encode()
        else:
            raw = str(result.get("output") or result.get("content") or json.dumps(result, ensure_ascii=False, indent=2)).encode()
        path.write_bytes(raw)
        return {**result, "artifact_path": str(path.relative_to(workspace)).replace("\\", "/"), "artifact_size": len(raw), "artifact_sha256": hashlib.sha256(raw).hexdigest(), "structured_result": result}

    async def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise HTTPException(404, detail="job not found")
        return self.jobs[job_id]

    async def cancel(self, job_id: str) -> Job:
        job = await self.get(job_id)
        task = self.tasks.get(job_id)
        if task and not task.done(): task.cancel()
        job.status = JobStatus.CANCELLED
        return job


job_service = JobService()
