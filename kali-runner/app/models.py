from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobRequest(BaseModel):
    run_id: str = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1)
    tool: str = Field(pattern="^(http_request|file_read|file_search|python_run)$")
    arguments: dict


class Job(BaseModel):
    job_id: str
    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    result: dict = Field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
