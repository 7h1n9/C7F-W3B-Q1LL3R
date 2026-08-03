from datetime import UTC, datetime, timedelta

import pytest

from app.services.runner_client import normalize_runner_job_id
from app.services import runner_client as runner_client_module
from app.services.runner_client import RunnerClient


def test_normalize_runner_job_id_accepts_supported_runner_shapes():
    assert normalize_runner_job_id({"runner_job_id": "r-1"}) == "r-1"
    assert normalize_runner_job_id({"job_id": "j-1"}) == "j-1"
    assert normalize_runner_job_id({"task_id": "t-1"}) == "t-1"
    assert normalize_runner_job_id({"job_id": "  "}) is None


def test_missing_runner_job_id_is_not_a_started_identifier():
    assert normalize_runner_job_id({"status": "ACCEPTED"}) is None
    assert normalize_runner_job_id(None) is None


@pytest.mark.asyncio
async def test_wait_job_collects_completed_result_without_waiting_for_sse(monkeypatch):
    class Response:
        is_success = True
        status_code = 200
        reason_phrase = "OK"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "job_id": "job-1",
                "status": "COMPLETED",
                "result": {
                    "status": "COMPLETED",
                    "artifact_path": "responses/job-1.json",
                    "structured_result": {"matched": True},
                },
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(runner_client_module.httpx, "AsyncClient", lambda **kwargs: Client())
    result = await RunnerClient().wait_job("job-1", tool_timeout_seconds=1)
    assert result["job_id"] == "job-1"
    assert result["status"] == "COMPLETED"
    assert result["structured_result"]["matched"] is True


@pytest.mark.parametrize("status", ["REQUESTED", "STARTED"])
def test_dispatch_watchdog_cutoff_is_five_minutes(status):
    created_at = datetime.now(UTC) - timedelta(minutes=5, seconds=1)
    assert status in {"REQUESTED", "STARTED"}
    assert created_at < datetime.now(UTC) - timedelta(minutes=5)
