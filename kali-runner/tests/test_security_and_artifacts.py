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
    response = client.post("/api/v1/jobs", json={"run_id": "r", "allowed_hosts": ["example.test"], "tool": "file_search", "arguments": {}})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_metadata_target_is_rejected_even_if_listed() -> None:
    request = JobRequest(run_id="r", allowed_hosts=["169.254.169.254"], tool="http_request", arguments={"url": "http://169.254.169.254/latest"})
    with pytest.raises(Exception) as error:
        await execute_http(request)
    assert getattr(error.value, "status_code", None) == 403


def test_runner_writes_relative_artifact(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = tmp_path / "run-a"; workspace.mkdir()
    request = JobRequest(run_id="run-a", allowed_hosts=["example.test"], tool="http_request", arguments={})
    job = Job(job_id="job-a", request=request)
    result = JobService()._persist_artifact(job, {"status_code": 200, "summary": "HTTP 200"})
    assert result["artifact_path"] == "responses/job-a.json"
    assert (workspace / result["artifact_path"]).is_file()


def test_workspace_api_upload_download_and_hash(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    client = TestClient(app)
    headers = {"X-Runner-Token": settings.api_token}
    assert client.post("/api/v1/workspaces/run-a", headers=headers).status_code == 200
    content = b"flag{runner-api}"
    import hashlib
    checksum = hashlib.sha256(content).hexdigest()
    response = client.put("/api/v1/workspaces/run-a/files/attachments/test.txt", headers={**headers, "X-Content-SHA256": checksum}, content=content)
    assert response.status_code == 200
    assert client.get("/api/v1/workspaces/run-a/files/attachments/test.txt", headers=headers).status_code == 404
    assert client.put("/api/v1/workspaces/run-a/files/outputs/test.txt", headers={**headers, "X-Content-SHA256": checksum}, content=content).status_code == 200
    downloaded = client.get("/api/v1/workspaces/run-a/files/outputs/test.txt", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.headers["x-artifact-sha256"] == checksum
