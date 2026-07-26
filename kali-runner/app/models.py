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
    allowed_hosts: list[str] = Field(default_factory=list)
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict


class JobResult(BaseModel):
    job_id: str | None = None
    status: JobStatus = JobStatus.COMPLETED
    error_code: str | None = None
    diagnostic_id: str | None = None
    tool_execution_completed: bool = False
    retryable: bool = False
    stage: str = "EXECUTION"
    exit_code: int | None = None
    summary: str = ""
    structured_result: dict = Field(default_factory=dict)
    artifact_paths: list[str] = Field(default_factory=list)
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    progress_path: str | None = None
    checkpoint_path: str | None = None


class Job(BaseModel):
    job_id: str
    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    result: dict = Field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    queue_position: int | None = None
