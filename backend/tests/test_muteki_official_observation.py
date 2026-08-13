import asyncio
import json
from types import SimpleNamespace

from app.solver.muteki.adapter.official_observation import (
    extract_native_observations,
    parse_safe_observation,
)
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.coordinator import MutekiCoordinator, _safe_unverified_attempt
from app.solver.muteki.worker.official_worker import OfficialWorkerResult


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="worker-1",
        intent_id="intent-1",
        challenge_id="run-1",
    )


def test_safe_observation_keeps_strategy_fields_and_drops_sensitive_fields() -> None:
    parsed = parse_safe_observation(
        json.dumps(
            {
                "tool": "sql_boolean_compare",
                "test_field": "department",
                "success": "true",
                "boolean_oracle_confirmed": True,
                "request_count": 3,
                "raw_response": "must not cross the adapter",
                "cookie": "must not cross the adapter",
            }
        ),
        fact_sequence=12,
    )

    assert parsed == {
        "tool": "sql_boolean_compare",
        "test_field": "department",
        "success": True,
        "boolean_oracle_confirmed": True,
        "request_count": 3,
        "source_fact_seq": 12,
    }


def test_safe_observation_accepts_official_verified_fact_envelope() -> None:
    parsed = parse_safe_observation(
        '[codex] VERIFIED_FACT={"tool":"sql_boolean_compare","success":true}',
        fact_sequence=13,
    )

    assert parsed == {
        "tool": "sql_boolean_compare",
        "success": True,
        "source_fact_seq": 13,
    }


def test_safe_observation_accepts_calibration_capabilities_only() -> None:
    parsed = parse_safe_observation(
        '[codex] VERIFIED_FACT={"tool":"oracle_expression_calibration",'
        '"success":true,"oracle_verified":true,'
        '"capabilities":["substring_supported"],'
        '"extraction_strategy":"bounded_binary","request_count":4}',
        fact_sequence=15,
    )

    assert parsed == {
        "tool": "oracle_expression_calibration",
        "success": True,
        "oracle_verified": True,
        "capabilities": ["substring_supported"],
        "extraction_strategy": "bounded_binary",
        "request_count": 4,
        "source_fact_seq": 15,
    }


def test_safe_observation_accepts_bounded_metadata_products_only() -> None:
    parsed = parse_safe_observation(
        json.dumps(
            {
                "tool": "mysql_metadata_discovery",
                "success": True,
                "stage": "tables",
                "target_expression": "information_schema.tables",
                "database": "asset_warranty",
                "tables": ["warranty_records", "users"],
                "columns": ["id", "status"],
                "request_count": 6,
                "raw_response": "must not cross the adapter",
                "target_expression_untrusted": "SELECT flag FROM secrets",
            }
        ),
        fact_sequence=16,
    )

    assert parsed == {
        "tool": "mysql_metadata_discovery",
        "success": True,
        "stage": "tables",
        "target_expression": "information_schema.tables",
        "database": "asset_warranty",
        "tables": ["warranty_records", "users"],
        "columns": ["id", "status"],
        "request_count": 6,
        "source_fact_seq": 16,
    }


def test_safe_observation_rejects_arbitrary_metadata_expression() -> None:
    parsed = parse_safe_observation(
        json.dumps(
            {
                "tool": "mysql_metadata_discovery",
                "success": True,
                "stage": "columns",
                "target_expression": "SELECT flag FROM secrets",
                "columns": ["flag"],
            }
        )
    )

    assert parsed == {
        "tool": "mysql_metadata_discovery",
        "success": True,
        "stage": "columns",
        "columns": ["flag"],
    }


def test_safe_observation_accepts_extraction_status_without_candidate_value() -> None:
    parsed = parse_safe_observation(
        json.dumps(
            {
                "tool": "boolean_config_extract",
                "success": True,
                "extraction_verified": True,
                "verification_method": "blackboard_flag_gate",
                "request_count": 4,
                "extracted_value": "must not cross the adapter",
                "flag": "must not cross the adapter",
            }
        ),
        fact_sequence=17,
    )

    assert parsed == {
        "tool": "boolean_config_extract",
        "success": True,
        "extraction_verified": True,
        "verification_method": "blackboard_flag_gate",
        "request_count": 4,
        "source_fact_seq": 17,
    }


def test_safe_observation_accepts_unavailable_attempt_feedback() -> None:
    parsed = parse_safe_observation(
        '{"tool":"sql_boolean_compare","test_field":"asset_no",'
        '"observation_status":"UNAVAILABLE"}'
    )

    assert parsed == {
        "tool": "sql_boolean_compare",
        "test_field": "asset_no",
        "observation_status": "UNAVAILABLE",
    }


def test_unverified_native_fact_cannot_cross_strategy_seam() -> None:
    assert extract_native_observations(
        [
            {
                "seq": 14,
                "kind": "fact_added",
                "verified": False,
                "payload": {
                    "fact": '{"tool":"sql_boolean_compare","success":true}',
                },
            }
        ],
        expected_action="sql_boolean_compare",
    ) == []


def test_sqlite_integer_verified_flag_is_accepted_but_zero_is_rejected() -> None:
    events = [
        {
            "seq": 20,
            "kind": "fact_added",
            "verified": 1,
            "artifact_id": "native-artifact",
            "payload": {
                "fact": '{"tool":"sql_boolean_compare","success":true}',
            },
        },
        {
            "seq": 21,
            "kind": "fact_added",
            "verified": 0,
            "artifact_id": "native-artifact",
            "payload": {
                "fact": '{"tool":"sql_boolean_compare","success":true}',
            },
        },
    ]

    observations = extract_native_observations(
        events,
        expected_action="sql_boolean_compare",
    )

    assert [item.fact_sequence for item in observations] == [20]


def test_native_fact_events_are_filtered_by_intent_and_projected() -> None:
    observations = extract_native_observations(
        [
            {
                "seq": 7,
                "kind": "fact_added",
                "artifact_id": "native-artifact",
                "payload": {
                    "intent_id": "other-intent",
                    "fact": '{"tool":"sql_boolean_compare","success":true}',
                },
            },
            {
                "seq": 8,
                "kind": "fact_added",
                "artifact_id": "native-artifact",
                "payload": {
                    "intent_id": "intent-1",
                    "fact": '{"tool":"sql_boolean_compare","test_field":"asset_no","success":true}',
                },
            },
        ],
        intent_id="intent-1",
        expected_action="sql_boolean_compare",
    )

    assert len(observations) == 1
    assert observations[0].fact_sequence == 8
    assert observations[0].source_evidence_refs == ("native-artifact",)
    assert observations[0].payload(evidence_refs=("evidence-1",)) == {
        "tool": "sql_boolean_compare",
        "source_fact_seq": 8,
        "test_field": "asset_no",
        "success": True,
        "evidence_refs": ["evidence-1"],
    }


def test_official_worker_runner_projects_structured_fact_before_generic_fallback() -> None:
    recorded: list[dict] = []

    class Graph:
        def native_event_cursor(self):
            return 10

        def native_fact_events_since(self, cursor):
            assert cursor == 10
            return [
                {
                    "seq": 11,
                    "kind": "fact_added",
                    "payload": {
                        "intent_id": "intent-1",
                        "fact": '{"tool":"sql_boolean_compare","test_field":"asset_no","success":true}',
                    },
                }
            ]

        def add_native_fact(self, **kwargs):
            recorded.append(kwargs)

        def conclude_intent(self, **kwargs):
            return True

    class Worker:
        async def execute(self, job):
            return OfficialWorkerResult(
                True,
                "COMPLETED",
                "codex",
                evidence_refs=("evidence-1",),
            )

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.official_worker = Worker()
    coordinator.official_worker_usage_bridge = None
    coordinator.graph = Graph()

    outcome = asyncio.run(coordinator._official_worker_runner(_job()))

    assert outcome.status == "COMPLETED"
    assert len(recorded) == 1
    assert recorded[0]["intent_id"] == "intent-1"
    assert recorded[0]["evidence_refs"] == ["evidence-1"]
    assert json.loads(recorded[0]["content"])["tool"] == "sql_boolean_compare"
    assert "raw" not in recorded[0]["content"]


def test_unstructured_native_fact_keeps_safe_generic_fallback() -> None:
    recorded: list[dict] = []

    class Graph:
        def native_event_cursor(self):
            return 3

        def native_fact_events_since(self, cursor):
            return [{"seq": 4, "kind": "fact_added", "payload": {"fact": "Worker prose"}}]

        def add_fact(self, **kwargs):
            recorded.append(kwargs)

        def conclude_intent(self, **kwargs):
            return True

    class Worker:
        async def execute(self, job):
            return OfficialWorkerResult(
                True,
                "COMPLETED",
                "codex",
                evidence_refs=("evidence-1",),
            )

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.official_worker = Worker()
    coordinator.official_worker_usage_bridge = None
    coordinator.graph = Graph()

    asyncio.run(coordinator._official_worker_runner(_job()))

    assert len(recorded) == 1
    assert recorded[0]["content"] == "Native Worker completed a bounded evidence-backed observation"


def test_unstructured_native_fact_keeps_safe_action_attempt_identity() -> None:
    job = _job()
    job.payload = {
        "tool_name": "sql_boolean_compare",
        "arguments": {"test_field": "asset_no"},
    }

    assert _safe_unverified_attempt(job) == {
        "tool": "sql_boolean_compare",
        "test_field": "asset_no",
        "observation_status": "UNAVAILABLE",
    }


def test_upstream_native_fact_is_linked_to_official_intent_product(tmp_path) -> None:
    challenge = SimpleNamespace(
        id="observation-challenge",
        name="Observation challenge",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )
    graph = UpstreamRuntimeGraph(
        tmp_path / "upstream.sqlite",
        challenge=challenge,
        challenge_id="observation-run",
    )
    try:
        intent_id = graph.propose_intent(
            actor="reason",
            description="test one bounded action",
        )
        assert graph.add_native_fact(
            actor="worker-1",
            content='{"tool":"http_request","success":true}',
            evidence_refs=["evidence-1"],
            intent_id=intent_id,
        ) > 0
        assert graph.native_graph().intent_products(intent_id)
        assert graph.snapshot()["facts"][-1]["evidence_refs"] == ["evidence-1"]
    finally:
        graph.close()
