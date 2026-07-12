import pytest

from app.orchestration.state_machine import RunStatus, restart, transition
from app.schemas.challenge import ChallengeInput


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
