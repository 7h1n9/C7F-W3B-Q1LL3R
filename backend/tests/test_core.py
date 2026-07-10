import pytest

from app.orchestration.state_machine import RunStatus, transition
from app.schemas.challenge import ChallengeInput


def test_challenge_requires_target_host_in_allowlist() -> None:
    with pytest.raises(ValueError):
        ChallengeInput(name="x", target_url="http://not-allowed.local", allowed_hosts=["allowed.local"])


def test_state_machine_rejects_invalid_transition() -> None:
    class Run: status = "CREATED"; current_phase = "CREATED"; started_at = None; finished_at = None
    with pytest.raises(Exception): transition(Run(), RunStatus.COMPLETED_SOLVED)


def test_state_machine_accepts_start() -> None:
    class Run: status = "CREATED"; current_phase = "CREATED"; started_at = None; finished_at = None
    run = Run(); transition(run, RunStatus.PREPARING)
    assert run.status == "PREPARING"
