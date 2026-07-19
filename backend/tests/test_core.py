import pytest

from app.orchestration.state_machine import RunStatus, restart, transition
from app.schemas.challenge import ChallengeInput
from app.services.context_builder import ContextBuilder


def test_challenge_requires_target_host_in_allowlist() -> None:
    with pytest.raises(ValueError):
        ChallengeInput(name="x", target_url="http://not-allowed.local", allowed_hosts=["allowed.local"])


def test_challenge_allows_url_style_host_list_values() -> None:
    challenge = ChallengeInput(
        name="x",
        target_url="http://not-allowed.local",
        allowed_hosts=["http://not-allowed.local;http://other.local"],
    )
    assert challenge.allowed_hosts == ["not-allowed.local", "other.local"]


def test_state_machine_rejects_invalid_transition() -> None:
    class Run: status = "CREATED"; current_phase = "CREATED"; started_at = None; finished_at = None
    with pytest.raises(Exception): transition(Run(), RunStatus.COMPLETED_SOLVED)


def test_state_machine_accepts_start() -> None:
    class Run: status = "CREATED"; current_phase = "CREATED"; started_at = None; finished_at = None
    run = Run(); transition(run, RunStatus.PREPARING)
    assert run.status == "PREPARING"


def test_state_machine_allows_controlled_reporting_from_execution() -> None:
    class Run:
        status = "EXECUTING"
        current_phase = "EXECUTING"
        started_at = object()
        finished_at = None

    run = Run()
    transition(run, RunStatus.REPORTING)
    assert run.status == "REPORTING"


def test_model_visible_role_snapshot_uses_live_permitted_tools() -> None:
    class Run:
        role_snapshot_json = {
            "name": "web-ctf-solver",
            "tools": ["http_request", "nmap_service_probe"],
        }

    snapshot = ContextBuilder._role_snapshot(Run(), {"http_request"})
    assert snapshot["tools"] == ["http_request"]
    assert snapshot["name"] == "web-ctf-solver"


def test_state_machine_restarts_failed_run_without_erasing_state() -> None:
    class Run:
        status = "FAILED_ENGINE"
        current_phase = "FAILED_ENGINE"
        started_at = object()
        finished_at = object()

    run = Run()
    previous = restart(run)
    assert previous == RunStatus.FAILED_ENGINE
    assert run.status == "WAITING_USER"
    assert run.current_phase == "WAITING_USER"
    assert run.finished_at is None
