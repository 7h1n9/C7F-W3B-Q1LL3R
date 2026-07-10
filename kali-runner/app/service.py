import asyncio
import uuid

from fastapi import HTTPException

from app.executors.kali_vm import KaliVmExecutionBackend
from app.models import Job, JobRequest, JobStatus


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
            job.result = await self.backend.execute(job.request)
            job.status = JobStatus.COMPLETED
        except Exception as error:
            job.status, job.error = JobStatus.FAILED, str(error)

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
