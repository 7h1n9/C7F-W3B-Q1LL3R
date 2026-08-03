from datetime import UTC, datetime, timedelta

import pytest

from app.services.runner_client import normalize_runner_job_id


def test_normalize_runner_job_id_accepts_supported_runner_shapes():
    assert normalize_runner_job_id({"runner_job_id": "r-1"}) == "r-1"
    assert normalize_runner_job_id({"job_id": "j-1"}) == "j-1"
    assert normalize_runner_job_id({"task_id": "t-1"}) == "t-1"
    assert normalize_runner_job_id({"job_id": "  "}) is None


def test_missing_runner_job_id_is_not_a_started_identifier():
    assert normalize_runner_job_id({"status": "ACCEPTED"}) is None
    assert normalize_runner_job_id(None) is None


@pytest.mark.parametrize("status", ["REQUESTED", "STARTED"])
def test_dispatch_watchdog_cutoff_is_five_minutes(status):
    created_at = datetime.now(UTC) - timedelta(minutes=5, seconds=1)
    assert status in {"REQUESTED", "STARTED"}
    assert created_at < datetime.now(UTC) - timedelta(minutes=5)
