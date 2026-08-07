from __future__ import annotations

from app.solver.blackboard import BlackboardState
from app.solver.knowledge import KnowledgeStore
from app.solver.observation import SolverObservation
from app.solver.reducers.web import WebObservationReducer


def test_http_observation_reduces_to_verified_response_and_endpoint_facts() -> None:
    observation = SolverObservation(
        action_name="http_request",
        success=True,
        raw_result={"status_code": 200, "body": "raw body must not persist"},
    )

    update = WebObservationReducer().reduce(observation)

    assert {item["type"] for item in update.verified_facts} == {
        "HTTP_RESPONSE",
        "HTTP_ENDPOINT_FOUND",
    }
    assert all(item["verified"] is True for item in update.verified_facts)
    assert update.next_phase == "VALIDATION"


def test_boolean_success_reduces_to_validation_success() -> None:
    observation = SolverObservation(
        action_name="sql_boolean_compare",
        success=True,
        raw_result={"true": True, "false": False},
    )

    update = WebObservationReducer().reduce(observation)

    assert {item["type"] for item in update.verified_facts} == {
        "BOOLEAN_ORACLE",
        "VALIDATION_SUCCESS",
    }
    assert update.next_phase == "EXPLOITATION"
    assert update.control_updates["strategy_needed"] is False


def test_boolean_failure_stays_in_validation_and_requires_strategy() -> None:
    observation = SolverObservation(
        action_name="sql_boolean_compare",
        success=True,
        raw_result={"true": False, "false": False},
    )

    update = WebObservationReducer().reduce(observation)

    assert update.verified_facts == []
    assert update.hypotheses[0]["type"] == "VALIDATION_INCONCLUSIVE"
    assert update.next_phase == "VALIDATION"
    assert update.control_updates["strategy_needed"] is True


def test_knowledge_store_keeps_cognition_but_discards_raw_observation() -> None:
    state = BlackboardState(
        run_id="knowledge-test",
        phase="BASELINE",
        knowledge={
            "target_url": "http://target/search",
            "observations": [{"response": "raw"}],
        },
    )
    update = WebObservationReducer().reduce(
        SolverObservation(
            action_name="http_request",
            success=True,
            raw_result={"status_code": 200, "body": "raw"},
        )
    )

    projected = KnowledgeStore().apply(state, update)

    assert projected.phase == "VALIDATION"
    assert projected.knowledge["target_url"] == "http://target/search"
    assert projected.knowledge["verified_facts"]
    assert "observations" not in projected.knowledge
    assert "raw" not in str(projected.knowledge)
