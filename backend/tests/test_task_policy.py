from app.security.task_policy import get_allowed_tools, validate_tools
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.orchestration.role_agent_runtime import RoleAgentRuntime
from app.schemas.multi_agent import AgentRole


def test_baseline_allows_http_request() -> None:
    policy = get_allowed_tools("SQL_INJECTION", "BASELINE")
    assert policy["phase"] == "BASELINE"
    assert policy["allowed_tools"] == ["http_request"]
    assert "content_discovery" in policy["forbidden_tools"]
    assert "sql_boolean_compare" in policy["forbidden_tools"]


def test_baseline_rejects_content_discovery() -> None:
    result = validate_tools("SQL_INJECTION", "BASELINE", ["content_discovery"])
    assert result["decision"] == "REVISE"
    assert result["invalid_tools"] == ["content_discovery"]
    assert "http_request" in result["reason"]


def test_validation_allows_boolean_oracle() -> None:
    result = validate_tools("SQL_INJECTION", "VALIDATION", ["sql_boolean_compare"])
    assert result["decision"] == "APPROVE"


def test_mapping_is_the_validation_boundary_for_sql_injection() -> None:
    result = validate_tools("SQL_INJECTION", "MAPPING", ["content_discovery"])
    assert result["decision"] == "REVISE"
    assert result["policy"]["phase"] == "VALIDATION"
    assert result["policy"]["allowed_tools"] == ["sql_boolean_compare", "oracle_probe_matrix"]


def test_invalid_planner_tool_is_revised() -> None:
    result = validate_tools("SQL_INJECTION", "BASELINE", ["content_discovery"])
    assert result == {
        "decision": "REVISE",
        "reason": "BASELINE phase only allows: http_request",
        "invalid_tools": ["content_discovery"],
        "policy": {
            "phase": "BASELINE",
            "allowed_tools": ["http_request"],
            "forbidden_tools": [
                "content_discovery",
                "sql_boolean_compare",
                "oracle_probe_matrix",
                "mysql_metadata_discovery",
                "sql_extract",
                "impact_validation",
                "report",
                "boolean_config_extract",
                "script_run",
                "http_compare",
            ],
        },
    }


def test_unknown_vulnerability_type_remains_compatible() -> None:
    result = validate_tools("GENERAL", "BASELINE", ["content_discovery"])
    assert result["decision"] == "APPROVE"
    assert result["reason"] == "NO_POLICY"


def test_planner_prompt_contains_phase_policy_context() -> None:
    task = AgentTask(
        id="AT-PLANNER-1",
        run_id="R-1",
        agent_role=AgentRole.PLANNER.value,
        task_kind="PLANNING",
        objective="Select the next bounded stage",
        context_json={
            "current_phase": "BASELINE",
            "task_policy": get_allowed_tools("SQL_INJECTION", "BASELINE"),
        },
    )
    challenge = Challenge(
        id="C-1",
        name="Golden SQL",
        target_url="http://target.test/search",
        metadata_json={"benchmark_case_id": "sql-injection-golden", "vulnerability_type": "SQL_INJECTION"},
    )
    policy = type("Policy", (), {"system_prompt": "Plan one bounded stage."})()
    prompt = RoleAgentRuntime()._prompt(task, policy, {}, challenge)
    assert '"current_phase": "BASELINE"' in prompt
    assert '"allowed_tools": ["http_request"]' in prompt
    assert '"forbidden_tools"' in prompt
