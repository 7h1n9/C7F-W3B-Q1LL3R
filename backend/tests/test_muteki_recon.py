from __future__ import annotations

import asyncio
import json

from app.solver.muteki.adapter.tool_adapter import ToolResult
from app.solver.muteki.core.race import RaceWorker
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.recon.breadth_scanner import BreadthScanner

BASE_URL = "http://target.test"


def _fake_http(calls: list[tuple[str, dict]]) :
    async def execute(tool_name: str, arguments: dict, workspace_id: str, run_id: str) -> ToolResult:
        calls.append((tool_name, dict(arguments)))
        if arguments.get("operation") == "create":
            return ToolResult(True, tool_name, {"summary": "session created"}, ("ev-session",))
        path = arguments["url"].removeprefix(BASE_URL) or "/"
        if path == "/":
            body = '<form><input type="password"></form><a href="/tickets">Tickets</a>'
            output = {"status_code": 200, "body": body, "headers": {"Server": "Flask"}, "cookie_names": ["session_id"]}
        elif path in {"/dashboard", "/tickets"}:
            output = {"status_code": 401, "body": "Login required", "headers": {"Location": "/login"}}
        else:
            output = {"status_code": 404, "body": "not found", "headers": {}}
        return ToolResult(True, tool_name, output, (f"ev-{path.strip('/') or 'root'}",))

    return execute


def test_breadth_scanner_uses_one_session_and_bounded_common_paths() -> None:
    calls: list[tuple[str, dict]] = []
    report = asyncio.run(BreadthScanner(_fake_http(calls), max_requests=8).scan(base_url=BASE_URL, workspace_id="ws", run_id="run"))

    target_calls = [item for item in calls if item[1].get("url")]
    assert len(target_calls) >= 5
    assert all(item[0] == "http_session_request" for item in calls)
    assert {item[1]["session_name"] for item in target_calls} == {"muteki-recon"}
    assert any(item.endpoint.endswith("/tickets") for item in report.observations)
    assert report.auth_required is True
    assert report.session_cookie_names == ("session_id",)


def test_race_writes_endpoint_facts_and_infers_idor(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    try:
        result = asyncio.run(
            RaceWorker(
                graph,
                _fake_http([]),
                target_url=BASE_URL,
                metadata={},
                workspace_id="ws",
                run_id="run",
            ).run()
        )
        contents = [json.loads(item.content) for item in graph.facts()]
        assert result.classification.classification == "IDOR"
        assert any(item["type"] == "ENDPOINTS_DISCOVERED" for item in contents)
        assert any(item["type"] == "AUTH_REQUIRED" and item["value"] for item in contents)
        assert any(item["type"] == "CHALLENGE_CLASSIFICATION" and item["classification"] == "IDOR" for item in contents)
    finally:
        graph.close()


def test_sql_metadata_takes_race_fast_path_without_http(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    calls: list[tuple[str, dict]] = []
    try:
        result = asyncio.run(
            RaceWorker(
                graph,
                _fake_http(calls),
                target_url=BASE_URL,
                metadata={"vulnerability_type": "SQL_INJECTION"},
                workspace_id="ws",
                run_id="run",
            ).run()
        )
        assert result.classification.classification == "SQLI"
        assert calls == []
        assert any("SQLI" in item.content for item in graph.facts())
    finally:
        graph.close()


def test_reason_blocks_sql_tools_before_classification(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    try:
        reason = MutekiReason(
            lambda _: [{"goal": "try sql_boolean_compare", "payload": {"tool_name": "sql_boolean_compare"}}],
            metadata={},
        )
        result = asyncio.run(reason.reason(graph))
        assert all(item.payload.get("tool_name") not in {"sql_boolean_compare", "sqlmap_run", "sqlmap_detect"} for item in result.intents)
        assert result.intents[0].goal == "CLASSIFY_CHALLENGE"
    finally:
        graph.close()


def test_reason_uses_idor_domain_after_explicit_classification(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    try:
        reason = MutekiReason(
            lambda _: [{"goal": "sql_boolean_compare", "payload": {"tool_name": "sql_boolean_compare"}}],
            metadata={"vulnerability_type": "IDOR"},
        )
        result = asyncio.run(reason.reason(graph))
        assert result.intents
        assert result.intents[0].payload["tool_name"] in {"http_session_request", "http_extract", "http_request"}
    finally:
        graph.close()


def test_reason_allows_sql_domain_only_for_explicit_sql_metadata(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    try:
        reason = MutekiReason(
            lambda _: [{"goal": "sql_boolean_compare", "payload": {"tool_name": "sql_boolean_compare"}}],
            metadata={"vulnerability_type": "SQL_INJECTION"},
        )
        result = asyncio.run(reason.reason(graph))
        assert result.intents[0].payload["tool_name"] == "sql_boolean_compare"
    finally:
        graph.close()


def test_reason_requires_classification_when_only_low_confidence_web_facts_exist(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    try:
        graph.add_fact(
            actor="race",
            content=json.dumps({"type": "ENDPOINTS_DISCOVERED", "endpoints": [{"endpoint": f"{BASE_URL}/", "status_code": 200}]}),
            evidence_refs=["ev-home"],
        )
        result = asyncio.run(MutekiReason(metadata={}).reason(graph))
        assert result.intents[0].goal == "CLASSIFY_CHALLENGE"
        assert result.intents[0].payload["tool_name"] == "http_request"
    finally:
        graph.close()
