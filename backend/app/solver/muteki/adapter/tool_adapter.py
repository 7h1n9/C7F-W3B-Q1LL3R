from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.solver.action import ActionIntent
from app.solver.worker.adapters.gateway import GatewayWorker
from app.tools.gateway import ToolGateway

from ..graph import Fact


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Sanitized result crossing from the production gateway into Muteki."""

    success: bool
    tool_name: str
    output: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    artifact_id: str | None = None
    tool_call_id: str | None = None
    error_code: str | None = None


class ToolAdapter:
    """Adapt a Muteki tool request to the existing ``GatewayWorker`` boundary.

    ``GatewayWorker`` remains the authority for tool policy, Runner dispatch,
    Artifact/Observation persistence, and EvidenceLedger creation.  This
    adapter only normalizes the result for the canonical graph.
    """

    def __init__(self, session: Any, run: Any, challenge: Any, *, tool_gateway: ToolGateway | None = None) -> None:
        self._worker = GatewayWorker(session, run, challenge, gateway=tool_gateway)

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_id: str,
        run_id: str,
    ) -> ToolResult:
        metadata = {
            "backend": "gateway",
            "run_id": str(run_id),
            "workspace_id": str(workspace_id),
        }
        action = ActionIntent(
            action_name=str(tool_name),
            reason="Muteki worker execution",
            parameters=dict(arguments),
            metadata=metadata,
        )
        result = await self._worker.execute(action)
        output = dict(result.output or {})
        if result.metadata.get("error_reason"):
            output["error_reason"] = str(result.metadata["error_reason"])[:800]
        return ToolResult(
            success=bool(result.success),
            tool_name=str(tool_name),
            output=output,
            evidence_refs=tuple(str(item) for item in result.evidence_refs or []),
            artifact_id=str(result.metadata.get("artifact_id")) if result.metadata.get("artifact_id") else (str(output.get("artifact_id")) if output.get("artifact_id") else None),
            tool_call_id=str(result.metadata.get("tool_call_id")) if result.metadata.get("tool_call_id") else (str(output.get("tool_call_id")) if output.get("tool_call_id") else None),
            error_code=str(result.metadata.get("error_code")) if result.metadata.get("error_code") else (str(output.get("error_code")) if output.get("error_code") else None),
        )

    @staticmethod
    def to_fact(tool_result: ToolResult, *, source_worker_id: str = "muteki-worker", request: Mapping[str, Any] | None = None) -> Fact:
        """Project a gateway result into a graph fact without raw response data."""
        summary = tool_result.output.get("summary") if isinstance(tool_result.output, Mapping) else None
        status = tool_result.output.get("status") if isinstance(tool_result.output, Mapping) else None
        content = f"tool={tool_result.tool_name}; success={tool_result.success}; status={status or ('SUCCESS' if tool_result.success else 'FAILED')}"
        request = request if isinstance(request, Mapping) else {}
        if request.get("method") or request.get("url"):
            content += f"; request_method={str(request.get('method') or 'GET').upper()}; request_url={str(request.get('url') or '')[:500]}"
        details = _safe_result_details(tool_result.output)
        if details:
            content += "; observed=" + json.dumps(details, ensure_ascii=False, sort_keys=True)
        if summary:
            content += f"; summary={str(summary)[:500]}"
        return Fact(
            fact_id=0,
            content=content,
            source_worker_id=source_worker_id,
            verified=bool(tool_result.success and tool_result.evidence_refs),
            created_at="",
            evidence_refs=tool_result.evidence_refs,
        )


def _safe_result_details(output: Mapping[str, Any]) -> dict[str, Any]:
    """Keep navigation metadata while excluding response bodies and secrets."""

    keys = (
        "status_code", "final_url", "links", "form_actions", "parameter_names",
        "json_keys", "tables", "columns", "databases", "database",
        "candidate_table", "candidate_column", "target_expression",
        "test_field", "oracle", "stage",
        "extracted_value", "flag_candidates", "oracle_verified",
        "boolean_oracle_confirmed", "adaptive_extraction_profile",
        "error_type", "error_reason",
    )
    details = {key: output.get(key) for key in keys if output.get(key) not in (None, [], {})}
    excerpt = output.get("body_excerpt") or output.get("content_excerpt")
    if isinstance(excerpt, str):
        try:
            parsed = json.loads(excerpt)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            report = parsed.get("diagnostic_report")
            if isinstance(report, Mapping) and report.get("download_url"):
                details["download_url"] = str(report["download_url"])[:500]
            for key in ("ticket_no", "title"):
                if parsed.get(key):
                    details[key] = str(parsed[key])[:200]
        html_forms = re.findall(r"<form\b[^>]*>(.*?)</form\s*>", excerpt, re.IGNORECASE | re.DOTALL)
        form_actions: list[str] = []
        form_methods: list[str] = []
        parameter_names: list[str] = []
        for form in html_forms[:20]:
            tag = re.search(r"<form\b[^>]*>", form, re.IGNORECASE)
            action = re.search(r"\baction=[\"']([^\"']+)", tag.group(0), re.IGNORECASE) if tag else None
            method = re.search(r"\bmethod=[\"']([^\"']+)", tag.group(0), re.IGNORECASE) if tag else None
            if action:
                form_actions.append(action.group(1)[:500])
            form_methods.append((method.group(1) if method else "GET").upper()[:10])
            parameter_names.extend(
                value[:80]
                for value in re.findall(
                    r"<(?:input|textarea|select)\b[^>]*\bname=[\"']([A-Za-z][A-Za-z0-9_.-]{0,79})",
                    form,
                    re.IGNORECASE,
                )
            )
        if form_actions:
            details["form_actions"] = list(dict.fromkeys(form_actions))[:20]
            details["form_methods"] = form_methods[:20]
        if parameter_names:
            details["parameter_names"] = list(dict.fromkeys(parameter_names))[:40]
    structured_forms = output.get("forms")
    if isinstance(structured_forms, list):
        form_methods: list[str] = []
        form_actions: list[str] = []
        parameter_names: list[str] = []
        for form in structured_forms[:20]:
            if not isinstance(form, Mapping):
                continue
            form_actions.extend(str(value)[:500] for value in (form.get("action"),) if value)
            form_methods.append(str(form.get("method") or "GET").upper()[:10])
            for field in form.get("inputs") or ():
                if isinstance(field, Mapping) and field.get("name"):
                    parameter_names.append(str(field["name"])[:80])
        if form_actions:
            details["form_actions"] = list(dict.fromkeys(form_actions))[:20]
            details["form_methods"] = form_methods[:20]
        if parameter_names:
            details["parameter_names"] = list(dict.fromkeys(parameter_names))[:40]
    links = details.get("links") or []
    ticket_links = [str(item) for item in links if re.search(r"/tickets/WO-[A-Za-z0-9-]+", str(item), re.I)]
    if ticket_links:
        details["ticket_links"] = ticket_links[:20]
    return details


__all__ = ["ToolAdapter", "ToolResult"]
