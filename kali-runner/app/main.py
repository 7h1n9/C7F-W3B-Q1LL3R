import asyncio
import secrets
import json

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from app.models import JobRequest
from app.service import job_service
from app.config import settings

app = FastAPI(title="CTF Kali Runner", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "execution_backend": "KaliVmExecutionBackend"}


def require_token(x_runner_token: str | None = Header(default=None)) -> None:
    if not settings.api_token or not x_runner_token or not secrets.compare_digest(x_runner_token, settings.api_token):
        raise HTTPException(401, detail="runner token required")


@app.post("/api/v1/jobs")
async def create_job(request: JobRequest, _: None = Depends(require_token)) -> dict:
    job = await job_service.create(request)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str, _: None = Depends(require_token)) -> dict:
    job = await job_service.get(job_id)
    return job.model_dump()


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: None = Depends(require_token)) -> dict:
    return (await job_service.cancel(job_id)).model_dump()


@app.get("/api/v1/jobs/{job_id}/events")
async def job_events(job_id: str, _: None = Depends(require_token)) -> StreamingResponse:
    async def stream():
        while True:
            job = await job_service.get(job_id)
            yield f"event: job.status\\ndata: {json.dumps({'status': job.status, 'error': job.error})}\\n\\n"
            if job.status.value in {"COMPLETED", "FAILED", "CANCELLED"}: return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")
