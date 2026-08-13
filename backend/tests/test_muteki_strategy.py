from __future__ import annotations

import json
from types import SimpleNamespace

from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime
from app.solver.muteki.strategy import MutekiStrategyPlanner

TARGET = "http://target.test/"


def _snapshot(*contents: str) -> dict:
    return {"facts": [{"content": value} for value in contents]}


def test_asset_warranty_strategy_starts_with_complete_boolean_contract() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/api/warranty/check",
            "method": "POST",
            "content_type": "application/json",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-1", "department": "OPS"},
        },
    )

    intent = planner.plan(_snapshot(json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100})))

    assert intent[0]["payload"]["tool_name"] == "sql_boolean_compare"
    arguments = intent[0]["payload"]["arguments"]
    assert arguments["request"]["json"]["asset_no"] == "PC-1"
    assert arguments["oracle"]["json_field"] == "matched"


def test_strategy_advances_from_boolean_evidence_to_calibration() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {"adapter": "asset_warranty", "dbms": "mysql", "endpoint": "/check", "fields": ["q"], "control_values": {"q": "x"}},
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        "tool=sql_boolean_compare; success=True; status=COMPLETED; observed=" + json.dumps({"boolean_oracle_confirmed": True}) + "; summary=ok",
    )
    snapshot["facts"][1]["evidence_refs"] = ["evidence-1"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "oracle_expression_calibration"
    assert intent[0]["payload"]["arguments"]["supporting_evidence_ids"] == ["evidence-1"]


def test_strategy_consumes_verified_native_typed_observation() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps(
            {
                "tool": "sql_boolean_compare",
                "test_field": "asset_no",
                "success": True,
                "boolean_oracle_confirmed": True,
                "oracle_verified": True,
                "request_count": 3,
            }
        ),
    )
    snapshot["facts"][1]["evidence_refs"] = ["official-artifact-1"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "oracle_expression_calibration"
    assert intent[0]["payload"]["arguments"]["supporting_evidence_ids"] == [
        "official-artifact-1"
    ]


def test_strategy_consumes_engine_enveloped_native_typed_observation() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    typed = json.dumps(
        {
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "success": True,
            "boolean_oracle_confirmed": True,
            "oracle_verified": True,
            "request_count": 3,
        },
        separators=(",", ":"),
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        f"[codex] {typed}",
    )
    snapshot["facts"][1]["evidence_refs"] = ["official-artifact-2"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "oracle_expression_calibration"
    assert intent[0]["payload"]["arguments"]["supporting_evidence_ids"] == [
        "official-artifact-2"
    ]


def test_strategy_moves_to_next_field_after_failed_boolean_attempt() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-1", "department": "OPS"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps(
            {
                "tool": "sql_boolean_compare",
                "test_field": "asset_no",
                "success": False,
                "boolean_oracle_confirmed": False,
                "oracle_verified": False,
                "request_count": 5,
            }
        ),
    )
    snapshot["facts"][1]["evidence_refs"] = ["failed-attempt-1"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "sql_boolean_compare"
    assert intent[0]["payload"]["arguments"]["test_field"] == "department"


def test_strategy_moves_to_next_field_after_unavailable_native_attempt() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-1", "department": "OPS"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "observation_status": "UNAVAILABLE",
        }),
    )
    snapshot["facts"][1]["evidence_refs"] = ["attempt-evidence"]
    snapshot["dead_ends"] = [{"description": "sql_boolean_compare produced no typed observation"}]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "sql_boolean_compare"
    assert intent[0]["payload"]["arguments"]["test_field"] == "department"


def test_strategy_retires_boolean_route_after_all_declared_fields_attempted() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-1", "department": "OPS"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "observation_status": "UNAVAILABLE",
        }),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "department",
            "success": False,
            "oracle_verified": False,
        }),
    )
    snapshot["facts"][1]["evidence_refs"] = ["attempt-evidence-1"]
    snapshot["facts"][2]["evidence_refs"] = ["attempt-evidence-2"]

    assert planner.plan(snapshot) == []


def test_strategy_consumes_calibration_typed_observation() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "success": True,
            "boolean_oracle_confirmed": True,
            "oracle_verified": True,
            "request_count": 3,
        }),
        "[codex] " + json.dumps({
            "tool": "oracle_expression_calibration",
            "success": True,
            "oracle_verified": True,
            "capabilities": ["substring_supported"],
            "extraction_strategy": "bounded_binary",
            "request_count": 4,
        }),
    )
    snapshot["facts"][1]["evidence_refs"] = ["boolean-evidence"]
    snapshot["facts"][2]["evidence_refs"] = ["calibration-evidence"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "mysql_metadata_discovery"
    assert intent[0]["payload"]["arguments"]["target_expression"] == "DATABASE()"


def test_strategy_consumes_typed_database_metadata_and_moves_to_tables() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "success": True,
            "boolean_oracle_confirmed": True,
            "oracle_verified": True,
            "request_count": 3,
        }),
        json.dumps({
            "tool": "oracle_expression_calibration",
            "success": True,
            "oracle_verified": True,
            "extraction_strategy": "bounded_binary",
            "request_count": 4,
        }),
        json.dumps({
            "tool": "mysql_metadata_discovery",
            "success": True,
            "stage": "database",
            "target_expression": "DATABASE()",
            "database": "asset_warranty",
            "request_count": 5,
        }),
    )
    snapshot["facts"][1]["evidence_refs"] = ["boolean-evidence"]
    snapshot["facts"][2]["evidence_refs"] = ["calibration-evidence"]
    snapshot["facts"][3]["evidence_refs"] = ["metadata-evidence"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "mysql_metadata_discovery"
    assert intent[0]["payload"]["arguments"]["target_expression"] == "information_schema.tables"


def test_strategy_consumes_typed_table_metadata_and_moves_to_columns() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "success": True,
            "boolean_oracle_confirmed": True,
            "oracle_verified": True,
            "request_count": 3,
        }),
        json.dumps({
            "tool": "oracle_expression_calibration",
            "success": True,
            "oracle_verified": True,
            "extraction_strategy": "bounded_binary",
            "request_count": 4,
        }),
        json.dumps({
            "tool": "mysql_metadata_discovery",
            "success": True,
            "stage": "tables",
            "target_expression": "information_schema.tables",
            "database": "asset_warranty",
            "tables": ["warranty_records"],
            "request_count": 7,
        }),
    )
    for index, ref in ((1, "boolean-evidence"), (2, "calibration-evidence"), (3, "metadata-evidence")):
        snapshot["facts"][index]["evidence_refs"] = [ref]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "mysql_metadata_discovery"
    assert intent[0]["payload"]["arguments"]["target_expression"] == "information_schema.columns"
    assert intent[0]["payload"]["arguments"]["candidate_table"] == "warranty_records"


def test_strategy_does_not_replay_completed_extraction_without_gated_flag() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/check",
            "fields": ["asset_no"],
            "control_values": {"asset_no": "PC-1"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        json.dumps({
            "tool": "sql_boolean_compare",
            "test_field": "asset_no",
            "success": True,
            "boolean_oracle_confirmed": True,
            "oracle_verified": True,
            "request_count": 3,
        }),
        json.dumps({
            "tool": "oracle_expression_calibration",
            "success": True,
            "oracle_verified": True,
            "extraction_strategy": "bounded_binary",
            "request_count": 4,
        }),
        json.dumps({
            "tool": "mysql_metadata_discovery",
            "success": True,
            "stage": "database",
            "target_expression": "DATABASE()",
            "database": "asset_warranty",
            "request_count": 5,
        }),
        json.dumps({
            "tool": "mysql_metadata_discovery",
            "success": True,
            "stage": "tables",
            "target_expression": "information_schema.tables",
            "database": "asset_warranty",
            "tables": ["warranty_records"],
            "request_count": 7,
        }),
        json.dumps({
            "tool": "mysql_metadata_discovery",
            "success": True,
            "stage": "columns",
            "target_expression": "information_schema.columns",
            "database": "asset_warranty",
            "tables": ["warranty_records"],
            "columns": ["value"],
            "request_count": 8,
        }),
        json.dumps({
            "tool": "boolean_config_extract",
            "success": True,
            "extraction_verified": True,
            "verification_method": "blackboard_flag_gate",
            "request_count": 4,
        }),
    )
    for index in range(1, len(snapshot["facts"])):
        snapshot["facts"][index]["evidence_refs"] = [f"evidence-{index}"]

    assert planner.plan(snapshot) == []


def test_strategy_tests_next_declared_field_when_oracle_is_not_confirmed() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-1", "department": "OPS"},
        },
    )
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SQLI", "confidence": 100}),
        "tool=sql_boolean_compare; success=True; status=COMPLETED; observed=" + json.dumps({
            "boolean_oracle_confirmed": False,
            "test_field": "asset_no",
        }),
    )
    snapshot["facts"][1]["evidence_refs"] = ["evidence-1"]

    intent = planner.plan(snapshot)

    assert intent[0]["payload"]["tool_name"] == "sql_boolean_compare"
    assert intent[0]["payload"]["arguments"]["test_field"] == "department"


def test_non_sql_classification_never_emits_sql_tools() -> None:
    for classification in ("FILE_UPLOAD", "JWT", "SSTI", "SSRF", "COMMAND_INJECTION", "XXE"):
        planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": classification})
        snapshot = _snapshot(
            json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": classification, "confidence": 85}),
            json.dumps({"type": "ENDPOINT_OBSERVED", "endpoint": TARGET + "api", "parameter_names": ["q"], "summary": "observed"}),
        )
        intents = planner.plan(snapshot)
        assert intents
        assert intents[0]["payload"]["tool_name"] not in {
            "sql_boolean_compare",
            "mysql_metadata_discovery",
            "boolean_config_extract",
            "sqlmap_run",
        }


def test_authenticated_non_sql_strategy_reuses_session() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "SSTI"})
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SSTI", "confidence": 85}),
        json.dumps({"type": "ENDPOINT_OBSERVED", "endpoint": TARGET + "templates", "parameter_names": ["name"], "summary": "template"}),
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={} ; summary=logged in",
    )

    intent = planner.plan(snapshot)[0]

    assert intent["payload"]["tool_name"] == "http_session_request"
    assert intent["payload"]["arguments"]["session_name"] == "muteki-recon"


def test_idor_strategy_owns_public_login_and_ticket_progression() -> None:
    planner = MutekiStrategyPlanner(
        TARGET,
        {"vulnerability_type": "IDOR"},
        public_credentials=("employee", "employee-pass"),
    )
    classification = json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "IDOR", "confidence": 85})

    login = planner.plan(_snapshot(classification))
    assert login[0]["payload"]["tool_name"] == "http_session_request"
    assert login[0]["payload"]["arguments"]["url"].endswith("/login")

    ticket_list = planner.plan(_snapshot(
        classification,
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={}",
    ))
    assert ticket_list[0]["payload"]["tool_name"] == "http_session_request"
    assert ticket_list[0]["payload"]["arguments"]["url"].endswith("/tickets")

    ticket_api = planner.plan(_snapshot(
        classification,
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={}",
        "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/tickets; observed=" + json.dumps({"links": ["/tickets/WO-1001"]}),
    ))
    assert ticket_api[0]["payload"]["arguments"]["url"].endswith("/api/tickets/WO-1001")


def test_path_traversal_strategy_owns_bounded_preview_variants() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "PATH_TRAVERSAL"})
    intents = planner.plan(_snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "PATH_TRAVERSAL", "confidence": 90}),
        json.dumps({
            "type": "ENDPOINT_OBSERVED",
            "endpoint": "http://target.test/preview?path=public/index.html",
            "disclosed_paths": ["archive/flag.txt"],
        }),
    ))

    assert intents[0]["payload"]["tool_name"] == "http_request"
    assert "../archive/flag.txt" in intents[0]["payload"]["arguments"]["url"]


def test_runtime_delegates_classified_web_lanes_to_strategy() -> None:
    runtime = MutekiRuntime.__new__(MutekiRuntime)
    runtime.challenge = SimpleNamespace(
        target_url=TARGET,
        metadata_json={"vulnerability_type": "IDOR"},
    )
    runtime._public_credentials = ("employee", "employee-pass")
    runtime._graph = None
    classification = json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "IDOR", "confidence": 85})

    login = runtime._reason_provider(_snapshot(classification))[0]
    assert login["payload"]["tool_name"] == "http_session_request"
    assert login["payload"]["arguments"]["url"].endswith("/login")

    ticket_list = runtime._reason_provider(_snapshot(
        classification,
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={}",
    ))[0]
    assert ticket_list["payload"]["tool_name"] == "http_session_request"
    assert ticket_list["payload"]["arguments"]["url"].endswith("/tickets")


def test_ssti_strategy_posts_to_observed_preview_form() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "SSTI"})
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SSTI", "confidence": 85}),
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={}",
        "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/templates/TPL-1; observed=" + json.dumps({
            "form_actions": ["/api/templates/TPL-1/preview"],
            "form_methods": ["POST"],
            "parameter_names": ["body"],
        }),
    )
    intent = planner.plan(snapshot)[0]
    assert intent["payload"]["tool_name"] == "http_session_request"
    assert intent["payload"]["arguments"]["method"] == "POST"
    assert intent["payload"]["arguments"]["url"] == "http://target.test/api/templates/TPL-1/preview"
    assert "{{7*7}}" in intent["payload"]["arguments"]["body"]


def test_ssti_inspects_observed_template_before_create_form() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "SSTI"})
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SSTI", "confidence": 85}),
        "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/login; observed={}",
        "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/templates; observed=" + json.dumps({
            "links": ["/templates", "/templates/new", "/templates/TPL-1"],
        }),
        "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/templates/new; observed=" + json.dumps({
            "form_actions": ["/api/templates"],
            "form_methods": ["POST"],
        }),
    )
    intent = planner.plan(snapshot)[0]
    assert intent["payload"]["tool_name"] == "http_session_request"
    assert intent["payload"]["arguments"]["method"] == "GET"
    assert intent["payload"]["arguments"]["url"].endswith("/templates/TPL-1")


def test_ssti_does_not_revisit_case_variant_template_url() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "SSTI"})
    snapshot = _snapshot(
        json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SSTI", "confidence": 85}),
        "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/templates/TPL-1; observed=" + json.dumps({
            "form_actions": ["/api/templates/TPL-1/preview"],
            "form_methods": ["POST"],
        }),
    )
    intent = planner.plan(snapshot)[0]
    assert intent["payload"]["arguments"]["method"] == "POST"
    assert intent["payload"]["arguments"]["url"].endswith("/api/templates/TPL-1/preview")


def test_ssti_advances_through_bounded_flag_probe_sequence() -> None:
    planner = MutekiStrategyPlanner(TARGET, {"vulnerability_type": "SSTI"})
    base = json.dumps({"type": "CHALLENGE_CLASSIFICATION", "classification": "SSTI", "confidence": 85})
    template = "tool=http_session_request; success=True; status=COMPLETED; request_method=GET; request_url=http://target.test/templates/TPL-1; observed=" + json.dumps({
        "form_actions": ["/api/templates/TPL-1/preview"],
        "form_methods": ["POST"],
    })

    first = planner.plan(_snapshot(base, template))[0]
    assert first["payload"]["arguments"]["body"] == "body={{7*7}}"

    second = planner.plan(_snapshot(base, template, "tool=http_session_request; success=True; status=COMPLETED; request_method=POST; request_url=http://target.test/api/templates/TPL-1/preview; observed={}"))[0]
    assert "config-key-1" in second["goal"]
    assert second["payload"]["arguments"]["body"] == "body={{ config['FLAG'] }}"
