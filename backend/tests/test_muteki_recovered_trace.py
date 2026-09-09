from __future__ import annotations

import json
import zipfile
from io import BytesIO

from app.services.muteki_poc_bundle import build_poc_bundle
from app.services.muteki_recovered_trace import load_recovered_trace, merge_recovered_state


def _write_trace(root, text: str) -> None:
    path = root / "muteki" / "shared" / "audit_export_worker.txt"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_recovered_trace_reads_verified_chain_and_dynamic_report_url(tmp_path) -> None:
    _write_trace(
        tmp_path,
        """Confirmed chain:
GET http://target.test/api/audit/events?department=all -> 200 JSON
POST /api/audit/export with form event_id=EV-1 -> 200 JSON download_url /reports/one
GET /reports/one -> 200 text/plain returned the flag
VERIFIED_FACT=GET /api/audit/events with department=all exposed an internal event.
POC_SAVE=shared/audit_export_worker.txt|python reproduce.py|available|Exact chain saved.
""",
    )

    trace = load_recovered_trace(workspace=tmp_path, target_url="http://target.test")

    assert trace["available"] is True
    assert len(trace["replayable_steps"]) == 3
    assert trace["replayable_steps"][1]["normalized_arguments"]["extract_json_path"] == {
        "save_as": "report_url",
        "path": "download_url",
    }
    assert trace["replayable_steps"][2]["normalized_arguments"]["url"] == "{{previous:report_url}}"
    assert trace["facts"][0]["verified"] is True
    assert trace["evidence"][0]["id"].startswith("recovered:")


def test_merge_recovered_state_only_adds_presentation_rows(tmp_path) -> None:
    _write_trace(tmp_path, "VERIFIED_FACT=POST /api/audit/export returned a report URL.\n")
    trace = load_recovered_trace(workspace=tmp_path, target_url="http://target.test")
    original = {"revision": 4, "facts": [], "key_conditions": [], "pocs": []}

    merged = merge_recovered_state(original, trace)

    assert original == {"revision": 4, "facts": [], "key_conditions": [], "pocs": []}
    assert merged["recovered"] is True
    assert len(merged["key_conditions"]) == 1
    assert merged["key_conditions"][0]["recovered"] is True


def test_recovered_steps_build_a_poc_bundle_without_legacy_toolcall_rows() -> None:
    steps = [
        {
            "order": 1,
            "title_zh": "导出事件",
            "purpose_zh": "验证导出接口",
            "tool_name": "http_request",
            "normalized_arguments": {
                "method": "POST",
                "url": "/api/audit/export",
                "form": {"event_id": "EV-1"},
                "extract_json_path": {"save_as": "report_url", "path": "download_url"},
            },
            "expected_status": 200,
            "expected_evidence": ["HTTP 200"],
            "source_artifact_ids": ["recovered:file:1"],
        },
        {
            "order": 2,
            "title_zh": "读取报告",
            "purpose_zh": "验证报告结果",
            "tool_name": "http_request",
            "normalized_arguments": {"method": "GET", "url": "{{previous:report_url}}"},
            "expected_status": 200,
            "expected_evidence": ["报告返回成功"],
            "source_artifact_ids": ["recovered:file:2"],
        },
    ]
    bundle = build_poc_bundle(
        run_id="run-recovered",
        challenge_name="Recovered challenge",
        steps=steps,
        evidence_by_tool_call={},
        evidence_by_artifact={
            "recovered:file:1": ["recovered:file:1"],
            "recovered:file:2": ["recovered:file:2"],
        },
    )

    with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
        plan = json.loads(archive.read("request-plan.json"))
        script = archive.read("reproduce.py").decode("utf-8")
        manifest = archive.read("evidence-manifest.json").decode("utf-8")

    assert len(plan) == 2
    assert "{{previous:report_url}}" in plan[1]["arguments"]["url"]
    assert "recovered:file:1" in manifest
    assert "previous response value is unavailable" in script
