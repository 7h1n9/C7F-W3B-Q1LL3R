from types import SimpleNamespace

import pytest

from app.services import tool_catalog_reconciler as reconciler_module
from app.services.tool_catalog_reconciler import ToolCatalogReconciler


class FakeSession:
    async def get(self, model, key):
        return SimpleNamespace(id=key)


class FakeCompiler:
    async def compile(self, *args):
        return SimpleNamespace(
            arguments={"fresh": True},
            arguments_digest="fresh-digest",
            tool_schema_hash="runtime-schema",
            compiler_name="test",
            compiler_version="test-v1",
        )


def make_objects():
    run = SimpleNamespace(id="run-1", recovery_checkpoint_json={})
    challenge = SimpleNamespace()
    attempt = SimpleNamespace()
    task = SimpleNamespace(context_json={})
    approved = SimpleNamespace(
        id="AA-1",
        tool_name="http_request",
        tool_schema_hash="cached-schema",
        proposal_id="proposal-1",
        analysis_review_id="review-1",
        compiled_arguments_json={"fresh": False},
        compiled_arguments_digest="old-digest",
        compile_status="COMPILED",
        status="ACTIVE",
    )
    return run, challenge, attempt, task, approved


@pytest.mark.asyncio
async def test_catalog_drift_refreshes_and_recompiles(monkeypatch):
    run, challenge, attempt, task, approved = make_objects()
    events = []

    async def append(*args, **kwargs):
        events.append(args[2])

    async def refresh(*args, **kwargs):
        return SimpleNamespace(
            id="manifest-1",
            effective_tools=["http_request"],
            schema_hashes={"http_request": "runtime-schema"},
            manifest_sha256="runtime-catalog-1",
        )

    monkeypatch.setattr(reconciler_module.event_service, "append", append)
    monkeypatch.setattr(reconciler_module, "load_tool_definitions", lambda: {})
    reconciler = ToolCatalogReconciler(manifest_refresher=refresh, compiler=FakeCompiler())
    result = await reconciler.reconcile(FakeSession(), run, challenge, attempt, task, approved)

    assert result.status == "RECOMPILED"
    assert run.recovery_checkpoint_json["tool_catalog_reconciliation"]["drift_count"] == 1
    assert approved.compiled_arguments_digest == "fresh-digest"
    assert "tool_catalog.refreshed" in events
    assert "approved_action.recompiled" in events


@pytest.mark.asyncio
async def test_third_catalog_drift_waits_for_user_without_wp(monkeypatch):
    run, challenge, attempt, task, approved = make_objects()

    async def append(*args, **kwargs):
        return None

    async def refresh(*args, **kwargs):
        return SimpleNamespace(
            id="manifest-unstable",
            effective_tools=[],
            schema_hashes={},
            manifest_sha256="unstable-catalog",
        )

    monkeypatch.setattr(reconciler_module.event_service, "append", append)
    monkeypatch.setattr(reconciler_module, "load_tool_definitions", lambda: {})
    reconciler = ToolCatalogReconciler(manifest_refresher=refresh, compiler=FakeCompiler())
    session = FakeSession()
    for _ in range(2):
        result = await reconciler.reconcile(session, run, challenge, attempt, task, approved)
        assert result.status == "RECOMPILED"
    result = await reconciler.reconcile(session, run, challenge, attempt, task, approved)

    assert result.status == "UNSTABLE"
    assert run.status == "WAITING_USER"
    assert run.last_error_code == "TOOL_CATALOG_UNSTABLE"
    assert "COMPLETED_UNSOLVED_WITH_WP" not in str(run.recovery_checkpoint_json)
