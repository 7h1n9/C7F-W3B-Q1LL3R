from __future__ import annotations

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
    def to_fact(tool_result: ToolResult, *, source_worker_id: str = "muteki-worker") -> Fact:
        """Project a gateway result into a graph fact without raw response data."""
        summary = tool_result.output.get("summary") if isinstance(tool_result.output, Mapping) else None
        status = tool_result.output.get("status") if isinstance(tool_result.output, Mapping) else None
        content = f"tool={tool_result.tool_name}; success={tool_result.success}; status={status or ('SUCCESS' if tool_result.success else 'FAILED')}"
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


__all__ = ["ToolAdapter", "ToolResult"]
