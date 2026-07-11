from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.executors.http_executor import execute_http
from app.main import app
from app.models import Job, JobRequest
from app.service import JobService


def test_runner_rejects_request_without_token() -> None:
    client = TestClient(app)
    response = client.post("/api/v1/jobs", json={"run_id": "r", "workspace_path": "x", "allowed_hosts": ["example.test"], "tool": "file_search", "arguments": {}})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_metadata_target_is_rejected_even_if_listed() -> None:
    request = JobRequest(run_id="r", workspace_path="x", allowed_hosts=["169.254.169.254"], tool="http_request", arguments={"url": "http://169.254.169.254/latest"})
    with pytest.raises(Exception) as error:
        await execute_http(request)
    assert getattr(error.value, "status_code", None) == 403


def test_runner_writes_relative_artifact(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = tmp_path / "run-a"; workspace.mkdir()
    request = JobRequest(run_id="run-a", workspace_path=str(workspace), allowed_hosts=["example.test"], tool="http_request", arguments={})
    job = Job(job_id="job-a", request=request)
    result = JobService()._persist_artifact(job, {"status_code": 200, "summary": "HTTP 200"})
    assert result["artifact_path"] == "responses/http_request_0001.json"
    assert (workspace / result["artifact_path"]).is_file()
