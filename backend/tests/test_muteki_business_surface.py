from __future__ import annotations

import asyncio
import json

from app.solver.muteki.adapter.tool_adapter import ToolResult
from app.solver.muteki.core.race import RaceWorker
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.recon.breadth_scanner import BreadthScanner, ReconObservation
from app.solver.muteki.recon.business_surface import derive_business_surface_facts


def _observation(**overrides: object) -> ReconObservation:
    values: dict[str, object] = {
        "endpoint": "http://target.test/",
        "status_code": 200,
        "summary": "HTTP 200",
        "evidence_refs": ("ev-1",),
    }
    values.update(overrides)
    return ReconObservation(**values)


def _fact_map(observations: list[ReconObservation]) -> dict[str, dict[str, object]]:
    return {item.fact_type: item.as_dict() for item in derive_business_surface_facts(observations)}


def test_procurement_surface_projects_objects_and_workflow_without_values() -> None:
    facts = _fact_map(
        [
            _observation(
                endpoint="http://target.test/orders",
                links=("http://target.test/orders/PO-1001?order_id=PO-1001&token=secret",),
                parameter_names=("order_id", "status"),
            )
        ]
    )

    assert "OBJECT_REFERENCE" in facts
    assert {item["kind"] for item in facts["OBJECT_REFERENCE"]["surfaces"][0]["references"]} >= {"order", "order_id"}
    assert "WORKFLOW_STATE" in facts
    serialized = json.dumps(facts, ensure_ascii=False)
    assert "secret" not in serialized
    assert '"token"' not in serialized


def test_approval_surface_projects_auth_boundary_and_state_keys() -> None:
    facts = _fact_map(
        [
            _observation(
                endpoint="http://target.test/approvals/42",
                status_code=302,
                redirected_to_login=True,
                cookie_names=("session_id",),
                parameter_names=("approval_id", "decision", "reviewer"),
            )
        ]
    )

    assert facts["AUTH_BOUNDARY"]["boundaries"][0]["auth_required"] is True
    assert set(facts["WORKFLOW_STATE"]["surfaces"][0]["state_keys"]) == {"decision", "reviewer"}
    assert facts["OBJECT_REFERENCE"]["surfaces"][0]["references"][-1]["value"] == "parameter"


def test_template_surface_projects_only_rendering_hints() -> None:
    facts = _fact_map(
        [
            _observation(
                endpoint="http://target.test/templates/preview",
                form_actions=("http://target.test/templates/render",),
                parameter_names=("template_id", "subject", "body"),
            )
        ]
    )

    surface = facts["TEMPLATE_SURFACE"]["surfaces"][0]
    assert {"template", "preview", "render", "subject", "body"} <= set(surface["hints"])
    assert "TEMPLATE_SURFACE" in facts


def test_document_surface_projects_xml_and_preview_signals() -> None:
    facts = _fact_map(
        [
            _observation(
                endpoint="http://target.test/contracts/preview",
                content_type="application/xml",
                parameter_names=("document_id", "file"),
                disclosed_paths=("archive/contracts/42.xml",),
                xml_detected=True,
            )
        ]
    )

    document = facts["DOCUMENT_PARSER_SURFACE"]["surfaces"][0]
    assert {"xml", "document_route", "disclosed_path_reference"} <= set(document["signals"])
    assert "archive/contracts/42.xml" not in json.dumps(facts, ensure_ascii=False)


def test_scanner_extracts_form_surface_metadata_without_persisting_body() -> None:
    async def execute(tool_name: str, arguments: dict, workspace_id: str, run_id: str) -> ToolResult:
        if arguments.get("operation") == "create":
            return ToolResult(True, tool_name, {}, ("ev-session",))
        body = '<form action="/templates/preview" method="POST"><input name="template_id"><textarea name="body"></textarea></form>'
        return ToolResult(
            True,
            tool_name,
            {"status_code": 200, "body": body, "headers": {"Content-Type": "text/html"}},
            ("ev-home",),
        )

    report = asyncio.run(BreadthScanner(execute, max_requests=4).scan(base_url="http://target.test", workspace_id="ws", run_id="run"))
    root = report.observations[0]
    assert root.form_actions == ("http://target.test/templates/preview",)
    assert root.form_methods == ("POST",)
    assert root.parameter_names == ("template_id", "body")
    facts = _fact_map(list(report.observations))
    assert "FORM_SURFACE" in facts
    assert "<form" not in json.dumps(facts, ensure_ascii=False)


def test_race_persists_typed_business_facts(tmp_path) -> None:
    async def execute(tool_name: str, arguments: dict, workspace_id: str, run_id: str) -> ToolResult:
        if arguments.get("operation") == "create":
            return ToolResult(True, tool_name, {}, ("ev-session",))
        path = arguments["url"].removeprefix("http://target.test") or "/"
        if path == "/":
            body = '<a href="/orders/PO-1001">Orders</a><form action="/templates/preview"><input name="template_id"></form>'
            return ToolResult(True, tool_name, {"status_code": 200, "body": body}, ("ev-home",))
        if path == "/orders/PO-1001":
            return ToolResult(True, tool_name, {"status_code": 401, "body": "login"}, ("ev-order",))
        return ToolResult(True, tool_name, {"status_code": 404, "body": "not found"}, (f"ev-{path.strip('/') or 'root'}",))

    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="business")
    try:
        asyncio.run(RaceWorker(graph, execute, target_url="http://target.test", workspace_id="ws", run_id="run").run())
        typed = [json.loads(item.content) for item in graph.facts() if json.loads(item.content).get("type") in {"ENDPOINT_CATALOG", "FORM_SURFACE", "OBJECT_REFERENCE", "AUTH_BOUNDARY", "WORKFLOW_STATE", "TEMPLATE_SURFACE", "DOCUMENT_PARSER_SURFACE"}]
        types = {item["type"] for item in typed}
        assert {"ENDPOINT_CATALOG", "FORM_SURFACE", "OBJECT_REFERENCE", "AUTH_BOUNDARY", "TEMPLATE_SURFACE"} <= types
        serialized = json.dumps(typed, ensure_ascii=False)
        assert "<a href" not in serialized
        assert "<form" not in serialized
    finally:
        graph.close()
