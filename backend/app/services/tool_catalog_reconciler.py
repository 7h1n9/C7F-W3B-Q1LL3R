"""Reconcile an ApprovedAction with the runtime tool catalog before dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, AnalysisReview, ApprovedAction, PlannerProposal
from app.models.run import RunAttempt, SolveRun
from app.services.approved_action_compiler import approved_action_compiler
from app.services.events import event_service
from app.services.tool_manifest import refresh_runtime_tool_manifest
from app.tools.registry import load_tool_definitions


@dataclass(frozen=True)
class CatalogReconciliation:
    status: str
    drift_count: int
    reason: str = ""
    manifest_id: str | None = None


class ToolCatalogReconciler:
    MAX_DRIFTS = 2

    def __init__(self, *, manifest_refresher: Callable[..., Awaitable[Any]] = refresh_runtime_tool_manifest, compiler=approved_action_compiler) -> None:
        self.manifest_refresher = manifest_refresher
        self.compiler = compiler

    async def reconcile(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, approved: ApprovedAction) -> CatalogReconciliation:
        manifest = await self.manifest_refresher(session, run, attempt, challenge, mcp_tools=[])
        definition = load_tool_definitions().get(approved.tool_name)
        runtime_hash = (manifest.schema_hashes or {}).get(approved.tool_name)
        consistent = (
            approved.tool_name in set(manifest.effective_tools or [])
            and definition is not None
            and definition.schema_hash() == approved.tool_schema_hash
            and (runtime_hash is None or runtime_hash == approved.tool_schema_hash)
        )
        if consistent:
            return CatalogReconciliation("CONSISTENT", 0, manifest_id=manifest.id)

        checkpoint = dict(run.recovery_checkpoint_json or {})
        state = checkpoint.get("tool_catalog_reconciliation")
        state = dict(state) if isinstance(state, dict) else {}
        drift_count = int(state.get("drift_count") or 0) + 1
        state.update({"drift_count": drift_count, "last_tool": approved.tool_name, "cached_schema_hash": approved.tool_schema_hash, "runtime_schema_hash": runtime_hash, "runtime_catalog_hash": manifest.manifest_sha256})
        checkpoint["tool_catalog_reconciliation"] = state
        run.recovery_checkpoint_json = checkpoint
        await event_service.append(session, run.id, "tool_catalog.drift", {"tool_name": approved.tool_name, "drift_count": drift_count, "cached_schema_hash": approved.tool_schema_hash, "runtime_schema_hash": runtime_hash, "runtime_catalog_hash": manifest.manifest_sha256})
        if drift_count > self.MAX_DRIFTS:
            run.status = "WAITING_USER"
            run.current_phase = "WAITING_USER"
            run.last_error_code = "TOOL_CATALOG_UNSTABLE"
            run.last_error_message = "The runtime tool catalog remained unstable after two reconciliations."
            run.recovery_checkpoint_json = {**checkpoint, "reason": "tool catalog drift exceeded the per-run refresh limit", "expected": ["refresh_catalog", "recompile_action", "retry_once"]}
            await event_service.append(session, run.id, "tool_catalog.unstable", {"drift_count": drift_count})
            return CatalogReconciliation("UNSTABLE", drift_count, "TOOL_CATALOG_UNSTABLE", manifest.id)

        proposal = await session.get(PlannerProposal, approved.proposal_id)
        review = await session.get(AnalysisReview, approved.analysis_review_id)
        if proposal is None or review is None:
            return CatalogReconciliation("UNSTABLE", drift_count, "APPROVED_ACTION_SOURCE_MISSING", manifest.id)
        compiled = await self.compiler.compile(session, run, challenge, proposal, review, approved.tool_name)
        approved.compiled_arguments_json = compiled.arguments
        approved.compiled_arguments_digest = compiled.arguments_digest
        approved.tool_schema_hash = compiled.tool_schema_hash
        approved.compiler_name = compiled.compiler_name
        approved.compiler_version = compiled.compiler_version
        approved.compile_status = "COMPILED"
        approved.status = "ACTIVE"
        task.context_json = {**(task.context_json or {}), "compiled_arguments_digest": compiled.arguments_digest}
        await event_service.append(session, run.id, "tool_catalog.refreshed", {"tool_name": approved.tool_name, "drift_count": drift_count, "manifest_id": manifest.id, "manifest_sha256": manifest.manifest_sha256})
        await event_service.append(session, run.id, "approved_action.recompiled", {"approved_action_id": approved.id, "tool_name": approved.tool_name, "drift_count": drift_count, "compiled_arguments_digest": compiled.arguments_digest})
        return CatalogReconciliation("RECOMPILED", drift_count, manifest_id=manifest.id)


tool_catalog_reconciler = ToolCatalogReconciler()
