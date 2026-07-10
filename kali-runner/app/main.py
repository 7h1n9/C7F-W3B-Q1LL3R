import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.models import JobRequest
from app.service import job_service

app = FastAPI(title="CTF Kali Runner", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "execution_backend": "KaliVmExecutionBackend"}


@app.post("/api/v1/jobs")
async def create_job(request: JobRequest) -> dict:
    job = await job_service.create(request)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = await job_service.get(job_id)
    return job.model_dump()


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    return (await job_service.cancel(job_id)).model_dump()


@app.get("/api/v1/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    async def stream():
        while True:
            job = await job_service.get(job_id)
            yield f"event: job.status\\ndata: {json.dumps({'status': job.status, 'error': job.error})}\\n\\n"
            if job.status.value in {"COMPLETED", "FAILED", "CANCELLED"}: return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")
