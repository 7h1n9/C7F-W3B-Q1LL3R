from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.models.multi_agent import AgentTask, AgentTaskResult, EvidenceLedger, VerifiedFact
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.solver.muteki import (
    EngineProfile,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)
from app.solver.muteki.adapter.official_evidence import SqlAlchemyOfficialEvidenceBridge
from app.solver.muteki.outcomes import DeadEndKind, DeadEndSignal
from app.solver.muteki.worker.official_worker import (
    OfficialWorkerAdapter,
    OfficialWorkerConfig,
    OfficialWorkerResult,
    _build_prompt,
    _container_target_url,
    _containerize_environment,
    _rewrite_target_urls,
    _typed_handoff_contract,
)


def _job(tmp_path):
    return SimpleNamespace(
        worker_id="worker-1",
        engine_id="codex",
        intent_id="intent-1",
        challenge_id="run-1",
        goal="inspect one bounded route",
        payload={"instruction": "write a fact"},
        environment={"MUTEKI_WORKSPACE": str(tmp_path), "SECRET": "must-not-forward"},
    )


def test_official_worker_stop_cancels_active_native_solver() -> None:
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="local"))
    calls: list[str] = []

    class ActiveSolver:
        def cancel(self) -> None:
            calls.append("cancel")

    adapter._active_solver = ActiveSolver()
    asyncio.run(adapter.stop())

    assert calls == ["cancel"]
    assert adapter._active_solver is None


def test_official_worker_uses_upstream_multiturn_modes(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeSolver:
        def __init__(self, **kwargs):
            seen.update({"max_turns": kwargs["max_turns"], "mode": kwargs["mode"]})

        async def run(self):
            return SimpleNamespace(solved=False, steps=2, session="session-1")

    monkeypatch.setattr("muteki.solver.cli_solver.CliSolver", FakeSolver)
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local", max_turns=80),
        shared_graph=object(),
    )

    race_job = _job(tmp_path)
    race_job.role = "race"
    result = adapter._execute_cli_solver_sync(race_job)

    assert result.success is True
    assert seen == {"max_turns": 80, "mode": "bootstrap"}


def test_official_worker_health_uses_projected_codex_home(tmp_path, monkeypatch) -> None:
    account_root = tmp_path / "accounts"
    codex_home = account_root / "codex-main" / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    seen: dict[str, object] = {}

    class Handle:
        run_id = "health-run"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        seen.update({"driver": driver.name, "argv": argv, "cwd": cwd, "timeout": timeout, "env": dict(env or {})})
        return SimpleNamespace(success=True, timed_out=False)

    monkeypatch.setattr("muteki.solver.container_exec.run_cli_container", fake_run_cli_container)
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="container", timeout_seconds=90),
    )
    adapter._container = Handle()
    adapter._account_root = str(account_root)

    healthy, reason = asyncio.run(
        adapter.health(EngineProfile("codex"), workspace=str(tmp_path / "workspace"))
    )

    assert healthy is True
    assert reason == ""
    assert seen["driver"] == "codex"
    assert seen["env"]["CODEX_HOME"] == "/run/muteki/accounts/codex-main/codex-home"
    assert "OPENAI_API_KEY" not in seen["env"]


def test_official_worker_health_accepts_upstream_cli_result(tmp_path, monkeypatch) -> None:
    class Handle:
        run_id = "health-cli-result"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        return SimpleNamespace(
            text="OK",
            timed_out=False,
            oom_killed=False,
            cancelled=False,
            runtime_status={"status": "finished", "rc": 0},
        )

    monkeypatch.setattr("muteki.solver.container_exec.run_cli_container", fake_run_cli_container)
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="container", timeout_seconds=180, health_timeout_seconds=90),
    )
    adapter._container = Handle()

    healthy, reason = asyncio.run(
        adapter.health(EngineProfile("codex"), workspace=str(tmp_path))
    )

    assert healthy is True
    assert reason == ""


def test_official_worker_health_rejects_nonzero_upstream_cli_result(tmp_path, monkeypatch) -> None:
    class Handle:
        run_id = "health-cli-failed"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        return SimpleNamespace(
            text="",
            timed_out=False,
            oom_killed=False,
            cancelled=False,
            runtime_status={"status": "finished", "rc": 1},
        )

    monkeypatch.setattr("muteki.solver.container_exec.run_cli_container", fake_run_cli_container)
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))
    adapter._container = Handle()

    healthy, reason = asyncio.run(
        adapter.health(EngineProfile("codex"), workspace=str(tmp_path))
    )

    assert healthy is False
    assert reason == "WORKER_HEALTHCHECK_FAILED"


def test_official_worker_preserves_usage_when_cli_solver_fails(tmp_path, monkeypatch) -> None:
    class FailingSolver:
        def __init__(self, **kwargs):
            self.cost = kwargs["cost"]

        async def run(self):
            await self.cost.add_external_usd(
                0.25,
                run_id="run-1",
                input_tokens=10,
                output_tokens=5,
            )
            raise RuntimeError("worker stopped after a metered turn")

    monkeypatch.setattr("muteki.solver.cli_solver.CliSolver", FailingSolver)
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local"),
        shared_graph=object(),
    )

    result = adapter._execute_cli_solver_sync(_job(tmp_path))

    assert result.success is False
    assert result.status == "FAILED"
    assert result.metadata["input_tokens"] == 10
    assert result.metadata["output_tokens"] == 5
    assert result.metadata["cost_usd"] == pytest.approx(0.25)
    assert result.metadata["calls"] == 1
    assert result.metadata["model"] == "codex"
    assert result.metadata["role"] == "worker"


def test_official_worker_flushes_partial_usage_on_recovery() -> None:
    from muteki.core.cost import CostController

    seen: list[tuple[object, OfficialWorkerResult]] = []

    async def usage_bridge(job, result):
        seen.append((job, result))

    adapter = OfficialWorkerAdapter(usage_bridge=usage_bridge)
    job = _job(Path("."))
    controller = CostController()
    asyncio.run(
        controller.add_external_usd(
            0.5,
            run_id="run-1",
            input_tokens=20,
            output_tokens=7,
        )
    )
    adapter._active_job = job
    adapter._active_cost_controller = controller

    asyncio.run(adapter.cancel_active())

    assert len(seen) == 1
    assert seen[0][1].status == "INTERRUPTED"
    assert seen[0][1].metadata["input_tokens"] == 20
    assert seen[0][1].metadata["output_tokens"] == 7
    assert seen[0][1].metadata["cost_usd"] == pytest.approx(0.5)

    asyncio.run(adapter.cancel_active())
    assert len(seen) == 1


def test_official_worker_recovery_flush_uses_actual_openai_model(tmp_path) -> None:
    from muteki.core.cost import CostController

    seen: list[OfficialWorkerResult] = []

    async def usage_bridge(_job, result):
        seen.append(result)

    adapter = OfficialWorkerAdapter(usage_bridge=usage_bridge)
    job = _job(tmp_path)
    job.engine_id = "openai-compatible:model-config-id"
    job.driver_profile = {
        "protocol": "chat_completions",
        "model": "deepseek-v4-flash",
    }
    controller = CostController()
    asyncio.run(controller.add_external_usd(0.01, run_id="run-1", input_tokens=2, output_tokens=3))
    execution = SimpleNamespace(job=job, cost_controller=controller)

    asyncio.run(adapter._flush_active_usage(execution))

    assert seen[0].metadata["model"] == "deepseek-v4-flash"


def test_official_worker_overdue_keys_are_scoped_to_old_executions() -> None:
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(timeout_seconds=10))
    old = _job(Path("."))
    old.worker_id = "old"
    recent = _job(Path("."))
    recent.worker_id = "recent"
    adapter._active_executions[adapter._job_key(old)] = SimpleNamespace(
        job=old, started_at=0.0
    )
    adapter._active_executions[adapter._job_key(recent)] = SimpleNamespace(
        job=recent, started_at=__import__("time").monotonic()
    )

    assert adapter._job_key(old) in adapter.overdue_job_keys()
    assert adapter._job_key(recent) not in adapter.overdue_job_keys()


def test_official_worker_cancellation_reaps_thread_execution(tmp_path) -> None:
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local", cancel_grace_seconds=1)
    )
    released = threading.Event()
    cancelled = threading.Event()

    class NativeHandle:
        def cancel(self) -> None:
            cancelled.set()
            released.set()

    def fake_execute(job):
        native = NativeHandle()
        adapter._set_native_handle(job, native)
        try:
            while not released.wait(0.01):
                pass
            return OfficialWorkerResult(False, "INTERRUPTED", "codex")
        finally:
            adapter._clear_native_handle(job, native)

    adapter._execute_sync = fake_execute  # type: ignore[method-assign]
    job = _job(tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(adapter.execute(job))
        await asyncio.sleep(0.05)
        await adapter.cancel_active()
        result = await task
        assert result.status == "INTERRUPTED"
        assert not adapter._active_executions

    asyncio.run(scenario())
    assert cancelled.is_set()


def test_official_worker_outer_task_cancel_reaps_native_thread(tmp_path) -> None:
    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local", cancel_grace_seconds=1)
    )
    released = threading.Event()
    cancelled = threading.Event()

    class NativeHandle:
        def cancel(self) -> None:
            cancelled.set()
            released.set()

    def fake_execute(job):
        native = NativeHandle()
        adapter._set_native_handle(job, native)
        try:
            while not released.wait(0.01):
                pass
            return OfficialWorkerResult(False, "INTERRUPTED", "codex")
        finally:
            adapter._clear_native_handle(job, native)

    adapter._execute_sync = fake_execute  # type: ignore[method-assign]
    job = _job(tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(adapter.execute(job))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not adapter._active_executions

    asyncio.run(scenario())
    assert cancelled.is_set()


def test_official_worker_adapter_projects_only_safe_step_metadata(tmp_path, monkeypatch) -> None:
    steps: list[dict[str, str]] = []
    adapter = OfficialWorkerAdapter(step_callback=steps.append)

    class Driver:
        def build_execute(self, prompt, session, **kwargs):
            assert "inspect one bounded route" in prompt
            assert kwargs["stream"] is True
            return ["fake-engine"]

    def fake_driver_for(name):
        assert name == "codex"
        return Driver()

    def fake_run(driver, argv, **kwargs):
        kwargs["on_step"](
            SimpleNamespace(
                kind="tool_result",
                tool="http_request",
                session="session-1",
                raw="secret response",
            )
        )
        return SimpleNamespace(
            text="bounded result",
            session="session-1",
            elapsed_s=1.0,
            timed_out=False,
            cancelled=False,
            steered=False,
            input_tokens=3,
            output_tokens=4,
        )

    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", fake_driver_for)
    monkeypatch.setattr("muteki.solver.cli_driver.run_cli_streaming", fake_run)

    result = asyncio.run(adapter.execute(_job(tmp_path)))

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.output == "bounded result"
    assert steps == [
        {
            "worker_id": "worker-1",
            "kind": "tool_result",
            "tool": "http_request",
            "session": "session-1",
        }
    ]


def test_official_worker_container_lifecycle_is_one_run_scoped(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_ensure(run_id, workspace, **kwargs):
        calls.append(("start", run_id))
        assert workspace == str(tmp_path)
        assert kwargs["network"] == "bridge"
        return "container-handle"

    def fake_teardown(run_id):
        calls.append(("stop", run_id))

    monkeypatch.setattr("muteki.solver.container_exec.ensure_container", fake_ensure)
    monkeypatch.setattr("muteki.solver.container_exec.teardown_container", fake_teardown)

    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="container", timeout_seconds=30),
    )
    asyncio.run(adapter.start(run_id="run-1", workspace=str(tmp_path)))
    assert adapter._container == "container-handle"
    asyncio.run(adapter.stop())
    assert calls == [("start", "run-1"), ("stop", "run-1")]


def test_official_worker_rewrites_private_host_target_only_for_container() -> None:
    target = "http://192.168.236.1:28346/api/warranty/check"
    container_target = _container_target_url(
        target,
        replacement_host="host.docker.internal",
    )

    assert container_target == "http://host.docker.internal:28346/api/warranty/check"
    assert _container_target_url(
        "https://public.example.test/check",
        replacement_host="host.docker.internal",
    ) == "https://public.example.test/check"
    assert _rewrite_target_urls(
        {
            "arguments": {
                "url": target,
                "follow_redirects": False,
                "body": "192.168.236.1 must not be rewritten",
            }
        },
        target,
        container_target,
    ) == {
        "arguments": {
            "url": container_target,
            "follow_redirects": False,
            "body": "192.168.236.1 must not be rewritten",
        }
    }


def test_official_worker_container_injects_projected_codex_home(tmp_path, monkeypatch) -> None:
    projection = tmp_path / "projection"
    (projection / "codex-main" / "codex-home").mkdir(parents=True)
    (projection / "codex-main" / "codex-home" / "auth.json").write_text(
        '{"tokens":{"access_token":"not-forwarded"}}', encoding="utf-8"
    )

    monkeypatch.setattr(
        "muteki.solver.container_exec.ensure_container",
        lambda run_id, workspace, **kwargs: SimpleNamespace(account_root=str(projection)),
    )
    monkeypatch.setattr("muteki.solver.container_exec.teardown_container", lambda run_id: None)

    seen: dict[str, str] = {}

    class Driver:
        def build_execute(self, prompt, session, **kwargs):
            return ["fake-engine"]

    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", lambda name: Driver())

    def fake_run(driver, argv, **kwargs):
        seen.update(kwargs["env"])
        return SimpleNamespace(
            text="bounded result",
            session="session-1",
            elapsed_s=1.0,
            timed_out=False,
            cancelled=False,
            steered=False,
            input_tokens=1,
            output_tokens=2,
        )

    monkeypatch.setattr("muteki.solver.cli_driver.run_cli_streaming", fake_run)
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))

    async def run() -> None:
        await adapter.start(run_id="run-1", workspace=str(tmp_path), account_root=str(tmp_path / "store"))
        job = _job(tmp_path)
        job.environment = {
            **job.environment,
            "MUTEKI_DEFAULT_ACCOUNT_ID": "codex-main",
        }
        result = await adapter.execute(job)
        assert result.success is True
        await adapter.stop()

    asyncio.run(run())

    assert seen["CODEX_HOME"] == "/run/muteki/accounts/codex-main/codex-home"
    assert "not-forwarded" not in str(seen)


def test_official_worker_prompt_projects_safe_action_contract() -> None:
    prompt = _build_prompt(
        "execute bounded request",
        "intent-1",
        {
            "tool_name": "sql_boolean_compare",
            "arguments": {
                "test_field": "department",
                "max_requests": 3,
                "body": {
                    "asset_no": "PC-2026-013",
                    "password": "must-not-forward",
                },
            },
        },
    )

    assert "sql_boolean_compare" in prompt
    assert "test_field" in prompt
    assert "max_requests" in prompt
    assert "asset_no" in prompt
    assert "MUTEKI_BLACKBOARD_SCRIPT" in prompt
    assert "boolean_oracle_confirmed" in prompt
    assert "Required Blackboard handoff" in prompt
    assert "write-fact" in prompt
    assert "--verified" in prompt
    assert "must-not-forward" not in prompt


def test_official_worker_prompt_requires_typed_fact_witness() -> None:
    payload = {
        "tool_name": "sql_boolean_compare",
        "arguments": {"test_field": "asset_no", "max_requests": 2},
    }
    prompt = _build_prompt("execute bounded request", "intent-1", payload)

    assert "OBSERVATION_JSON=" in prompt
    assert "VERIFIED_FACT=" in prompt
    assert "non-marker safe witness sentence" in prompt
    assert "Typed Fact shape:" in prompt
    assert _typed_handoff_contract(payload) == {
        "tool": "sql_boolean_compare",
        "test_field": "asset_no",
        "success": "<true-or-false>",
        "boolean_oracle_confirmed": "<true-or-false>",
        "oracle_verified": "<true-or-false>",
        "request_count": "<integer>",
    }


def test_official_worker_calibration_handoff_is_typed() -> None:
    payload = {
        "tool_name": "oracle_expression_calibration",
        "arguments": {"dbms": "mysql", "max_calibration_requests": 8},
    }

    assert _typed_handoff_contract(payload) == {
        "tool": "oracle_expression_calibration",
        "success": "<true-or-false>",
        "oracle_verified": "<true-or-false>",
        "capabilities": ["<capability>"],
        "extraction_strategy": "<identifier>",
        "request_count": "<integer>",
    }


def test_official_worker_metadata_handoff_is_typed_and_bounded() -> None:
    payload = {
        "tool_name": "mysql_metadata_discovery",
        "arguments": {
            "stage": "tables",
            "target_expression": "information_schema.tables",
            "max_requests": 12,
        },
    }

    expected = {
        "tool": "mysql_metadata_discovery",
        "success": "<true-or-false>",
        "stage": "<database-or-tables-or-columns>",
        "target_expression": "<DATABASE()-or-information_schema.tables-or-information_schema.columns>",
        "database": "<identifier>",
        "tables": ["<identifier>"],
        "columns": ["<identifier>"],
        "request_count": "<integer>",
    }

    assert _typed_handoff_contract(payload) == expected


def test_official_worker_extraction_handoff_keeps_flag_inside_official_gate() -> None:
    payload = {
        "tool_name": "boolean_config_extract",
        "arguments": {"target_expression": "SELECT value FROM warranty_records LIMIT 1"},
    }

    assert _typed_handoff_contract(payload) == {
        "tool": "boolean_config_extract",
        "success": "<true-or-false>",
        "extraction_verified": "<true-or-false>",
        "verification_method": "<artifact_witness-or-blackboard_flag_gate>",
        "request_count": "<integer>",
    }
    prompt = _build_prompt("extract one bounded candidate", "intent-flag", payload)
    assert "write-flag" in prompt
    assert "Never place the candidate" in prompt


def test_official_worker_maps_blackboard_paths_into_container_workspace(tmp_path) -> None:
    host_workspace = tmp_path / "run" / "muteki"
    blackboard = host_workspace / "graph" / "upstream_shared_graph.db"
    handle = SimpleNamespace(
        to_container_path=lambda value: (
            "/home/kali/workspace"
            if str(value) == str(host_workspace)
            else "/home/kali/workspace/"
            + str(value)[len(str(host_workspace)) + 1 :]
            .replace("/", "\\")
        ),
    )

    mapped = _containerize_environment(
        {
            "MUTEKI_WORKSPACE": str(host_workspace),
            "MUTEKI_BLACKBOARD_DB": str(blackboard),
            "MUTEKI_WORKER_ID": "worker-1",
            "MUTEKI_INTENT_ID": "intent-1",
        },
        handle,
    )

    assert mapped["MUTEKI_WORKSPACE"] == "/home/kali/workspace"
    assert mapped["MUTEKI_BLACKBOARD_DB"] == "/home/kali/workspace/graph/upstream_shared_graph.db"
    assert mapped["MUTEKI_WORKER_ID"] == "worker-1"
    assert mapped["MUTEKI_INTENT_ID"] == "intent-1"
    assert mapped["MUTEKI_BLACKBOARD_SCRIPT"] == "/usr/local/bin/blackboard.py"


def test_official_worker_preserves_already_containerized_paths() -> None:
    values = {
        "MUTEKI_WORKSPACE": "/home/kali/workspace",
        "MUTEKI_BLACKBOARD_DB": "/home/kali/workspace/graph/board.sqlite",
    }

    assert _containerize_environment(values, SimpleNamespace(to_container_path=lambda _: "wrong")) == {
        **values,
        "MUTEKI_BLACKBOARD_SCRIPT": "/usr/local/bin/blackboard.py",
    }


def test_official_worker_bridge_returns_only_durable_evidence_refs(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, str, str | None, str]] = []

    class Bridge:
        async def ingest(self, *, run_id, worker_id, intent_id, result):
            seen.append((run_id, worker_id, intent_id, result.output))
            return [" artifact-1 ", "artifact-1", "artifact-2"]

    class Driver:
        def build_execute(self, prompt, session, **kwargs):
            return ["fake-engine"]

    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", lambda name: Driver())
    monkeypatch.setattr(
        "muteki.solver.cli_driver.run_cli_streaming",
        lambda driver, argv, **kwargs: SimpleNamespace(
            text="native result",
            session="session-1",
            elapsed_s=1.0,
            timed_out=False,
            cancelled=False,
            steered=False,
            input_tokens=1,
            output_tokens=2,
        ),
    )

    result = asyncio.run(OfficialWorkerAdapter(evidence_bridge=Bridge()).execute(_job(tmp_path)))

    assert result.success is True
    assert result.evidence_refs == ("artifact-1", "artifact-2")
    assert seen == [("run-1", "worker-1", "intent-1", "native result")]


def test_official_worker_bridge_failure_fails_closed_without_exception(tmp_path, monkeypatch) -> None:
    class BrokenBridge:
        async def ingest(self, **kwargs):
            raise RuntimeError("secret target response")

    class Driver:
        def build_execute(self, prompt, session, **kwargs):
            return ["fake-engine"]

    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", lambda name: Driver())
    monkeypatch.setattr(
        "muteki.solver.cli_driver.run_cli_streaming",
        lambda driver, argv, **kwargs: SimpleNamespace(
            text="native result",
            session="session-1",
            elapsed_s=1.0,
            timed_out=False,
            cancelled=False,
            steered=False,
            input_tokens=1,
            output_tokens=2,
        ),
    )

    result = asyncio.run(OfficialWorkerAdapter(evidence_bridge=BrokenBridge()).execute(_job(tmp_path)))

    assert result.success is False
    assert result.status == "EVIDENCE_BRIDGE_FAILED"
    assert result.evidence_refs == ()
    assert "secret target response" not in str(result.metadata)


def test_official_worker_empty_bridge_result_fails_closed(tmp_path, monkeypatch) -> None:
    class EmptyBridge:
        async def ingest(self, **kwargs):
            return []

    class Driver:
        def build_execute(self, prompt, session, **kwargs):
            return ["fake-engine"]

    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", lambda name: Driver())
    monkeypatch.setattr(
        "muteki.solver.cli_driver.run_cli_streaming",
        lambda driver, argv, **kwargs: SimpleNamespace(
            text="native result",
            session="session-1",
            elapsed_s=1.0,
            timed_out=False,
            cancelled=False,
            steered=False,
            input_tokens=1,
            output_tokens=2,
        ),
    )

    result = asyncio.run(OfficialWorkerAdapter(evidence_bridge=EmptyBridge()).execute(_job(tmp_path)))

    assert result.success is False
    assert result.status == "EVIDENCE_BRIDGE_EMPTY"
    assert result.evidence_refs == ()


def test_official_worker_bridge_ingests_failed_dead_end_with_artifact(tmp_path, monkeypatch) -> None:
    seen: list[str] = []
    artifact_path = tmp_path / ".muteki-artifacts" / "chat-worker-1" / "http-evidence.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"records":[{"method":"GET","url":"http://target/"}]}', encoding="utf-8")
    dead_end = DeadEndSignal(
        kind=DeadEndKind.ROUTE_DEAD_END,
        reason="route rejected",
    )

    class Bridge:
        async def ingest(self, *, run_id, worker_id, intent_id, result):
            seen.append(result.status)
            return ["evidence-dead-end"]

    adapter = OfficialWorkerAdapter(evidence_bridge=Bridge())
    monkeypatch.setattr(
        adapter,
        "_execute_sync",
        lambda _job: OfficialWorkerResult(
            False,
            "ROUTE_DEAD_END",
            "codex",
            evidence_artifact_path=str(artifact_path),
            dead_end_signal=dead_end,
        ),
    )

    result = asyncio.run(adapter.execute(_job(tmp_path)))

    assert seen == ["ROUTE_DEAD_END"]
    assert result.success is False
    assert result.dead_end_signal == dead_end
    assert result.evidence_refs == ("evidence-dead-end",)


def test_coordinator_rejects_native_success_without_evidence_reference() -> None:
    class Graph:
        def __init__(self):
            self.dead_ends: list[str] = []

        def add_dead_end(self, *, actor, description):
            self.dead_ends.append(description)

    class Worker:
        async def execute(self, job):
            return OfficialWorkerResult(True, "COMPLETED", "codex")

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.official_worker = Worker()
    coordinator.graph = Graph()

    outcome = asyncio.run(coordinator._official_worker_runner(_job("workspace")))

    assert outcome.status == "FAILED"
    assert outcome.result == "EVIDENCE_BRIDGE_REQUIRED"
    assert outcome.result_code == "EXTERNAL_BLOCKER"
    assert coordinator.graph.dead_ends == []


def test_coordinator_projects_native_evidence_refs_without_raw_output(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "native.sqlite", challenge_id="run-native")
    graph.propose_intent(actor="reason", intent_id="intent-1", description="inspect route")
    assert graph.claim_intent(worker="worker-1", intent_id="intent-1")

    class Worker:
        async def execute(self, job):
            return OfficialWorkerResult(
                True,
                "COMPLETED",
                "codex",
                output="secret raw worker response must not enter graph",
                evidence_refs=("evidence-native-1",),
            )

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.official_worker = Worker()
    coordinator.graph = graph

    outcome = asyncio.run(coordinator._official_worker_runner(_job(tmp_path)))

    assert outcome.status == "COMPLETED"
    facts = graph.facts()
    assert len(facts) == 1
    assert facts[0].content == "Native Worker completed a bounded evidence-backed observation"
    assert facts[0].evidence_refs == ("evidence-native-1",)
    assert "secret raw worker response" not in facts[0].content
    assert graph.intents(status="done")[0].intent_id == "intent-1"
    graph.close()


def test_coordinator_projects_safe_openai_worker_failure_reason() -> None:
    class Graph:
        def __init__(self):
            self.dead_ends: list[str] = []

        def add_dead_end(self, *, actor, description):
            self.dead_ends.append(description)

    class Worker:
        async def execute(self, job):
            return OfficialWorkerResult(
                False,
                "FAILED",
                "step-3.7-flash",
                output="OPENAI_COMPATIBLE_WORKER_FAILED",
            )

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.official_worker = Worker()
    coordinator.graph = Graph()

    outcome = asyncio.run(coordinator._official_worker_runner(_job("workspace")))

    assert outcome.status == "FAILED"
    assert outcome.result_code == "WORKER_FAILURE"
    # Provider/transport failure is not evidence that the target route is a
    # dead end.  It remains retryable execution telemetry.
    assert coordinator.graph.dead_ends == []


def test_sqlalchemy_native_evidence_bridge_uses_existing_chain(tmp_path) -> None:
    async def run_case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        run = SimpleNamespace(id="run-native")
        result = OfficialWorkerResult(True, "COMPLETED", "codex", output="bounded evidence")

        async with factory() as session:
            bridge = SqlAlchemyOfficialEvidenceBridge(session, run, workspace)
            refs = await bridge.ingest(
                run_id="run-native",
                worker_id="worker-native",
                intent_id="intent-native",
                result=result,
            )
            repeated = await bridge.ingest(
                run_id="run-native",
                worker_id="worker-native",
                intent_id="intent-native",
                result=result,
            )
            evidence = await session.scalar(select(EvidenceLedger).where(EvidenceLedger.id == refs[0]))

            assert refs == repeated
            assert evidence is not None
            assert evidence.evidence_type == "MUTEKI_NATIVE_WORKER"
            assert (workspace / "evidence/native-workers").is_dir()
            assert run.tool_call_count == 1
            assert run.run_total_logical_tool_calls == 1
            assert run.run_total_agent_steps == 1

        await engine.dispose()

    asyncio.run(run_case())


def test_sqlalchemy_native_evidence_bridge_projects_run_counters(tmp_path) -> None:
    async def run_case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace = tmp_path / "workspace-counters"
        workspace.mkdir()

        async with factory() as session:
            run = SolveRun(challenge_id="challenge-counters", workspace_path=str(workspace))
            session.add(run)
            await session.flush()
            bridge = SqlAlchemyOfficialEvidenceBridge(session, run, workspace)
            await bridge.ingest(
                run_id=str(run.id),
                worker_id="worker-native",
                intent_id="intent-native",
                result=OfficialWorkerResult(
                    True,
                    "COMPLETED",
                    "codex",
                    output="bounded evidence",
                    metadata={"num_turns": 3},
                ),
            )

            await session.refresh(run)
            assert run.tool_call_count == 1
            assert run.run_total_logical_tool_calls == 1
            assert run.run_total_agent_steps == 3
            assert run.attempt_agent_steps == 3

        await engine.dispose()

    asyncio.run(run_case())


def test_sqlalchemy_native_evidence_bridge_consumes_official_artifact(tmp_path) -> None:
    async def run_case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace = tmp_path / "workspace-artifact"
        workspace.mkdir()
        native_artifact = workspace / ".muteki-artifacts" / "whole-run.txt"
        native_artifact.parent.mkdir()
        native_artifact.write_text("official artifact content", encoding="utf-8")
        run = SimpleNamespace(id="run-native-artifact")
        result = OfficialWorkerResult(
            True,
            "COMPLETED",
            "codex",
            output="compatibility summary",
            evidence_artifact_path=str(native_artifact),
        )

        async with factory() as session:
            bridge = SqlAlchemyOfficialEvidenceBridge(session, run, workspace)
            refs = await bridge.ingest(
                run_id="run-native-artifact",
                worker_id="worker-native",
                intent_id="intent-native",
                result=result,
            )
            evidence = await session.scalar(select(EvidenceLedger).where(EvidenceLedger.id == refs[0]))
            assert evidence is not None
            artifact = await session.scalar(select(Artifact).where(Artifact.id == evidence.artifact_id))
            assert artifact is not None
            stored = workspace / str(artifact.file_path)
            # Evidence stores the protected artifact under its own relative
            # path; the graph never receives the source content.
            assert stored.exists()
            assert stored.read_text(encoding="utf-8") == "official artifact content"

        await engine.dispose()

    asyncio.run(run_case())


def test_sqlalchemy_native_evidence_bridge_projects_structured_http_records(tmp_path) -> None:
    async def run_case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace = tmp_path / "workspace-structured"
        workspace.mkdir()
        native_artifact = workspace / ".muteki-artifacts" / "chat-worker-native" / "http-evidence.json"
        native_artifact.parent.mkdir(parents=True)
        records = [
            {
                "tool": "http_request",
                "method": "GET",
                "url": "http://target.test/first",
                "status_code": 200,
                "final_url": "http://target.test/first",
                "headers": {"Content-Type": "text/html", "Set-Cookie": "session=secret-one"},
                "body": "secret-body-one",
            },
            {
                "tool": "http_session_request",
                "method": "POST",
                "url": "http://target.test/second",
                "status_code": 403,
                "final_url": "http://target.test/denied",
                "headers": {"Content-Type": "application/json"},
                "body": "secret-body-two",
            },
        ]
        native_artifact.write_text(
            json.dumps(
                {
                    "worker_id": "worker-native",
                    "intent_id": "intent-native",
                    "records": records,
                }
            ),
            encoding="utf-8",
        )

        async with factory() as session:
            run = SolveRun(challenge_id="challenge-structured", workspace_path=str(workspace))
            session.add(run)
            await session.flush()
            bridge = SqlAlchemyOfficialEvidenceBridge(session, run, workspace)
            result = OfficialWorkerResult(
                True,
                "COMPLETED",
                "codex",
                evidence_artifact_path=str(native_artifact),
                metadata={"num_turns": 2},
            )
            refs = await bridge.ingest(
                run_id=str(run.id),
                worker_id="worker-native",
                intent_id="intent-native",
                result=result,
            )
            repeated = await bridge.ingest(
                run_id=str(run.id),
                worker_id="worker-native",
                intent_id="intent-native",
                result=result,
            )

            assert refs == repeated
            assert len(refs) == 2

            tool_calls = list(
                (
                    await session.scalars(
                        select(ToolCall).where(
                            ToolCall.run_id == str(run.id),
                            ToolCall.execution_layer == "muteki_native",
                        )
                    )
                ).all()
            )
            tool_calls.sort(key=lambda item: item.arguments_json["record_index"])
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.run_id == str(run.id),
                            Artifact.artifact_type == "muteki_native_http_record",
                        )
                    )
                ).all()
            )
            observations = list(
                (
                    await session.scalars(
                        select(Observation).where(
                            Observation.run_id == str(run.id),
                            Observation.observation_type == "MUTEKI_NATIVE_HTTP_OBSERVATION",
                        )
                    )
                ).all()
            )
            evidence_rows = list(
                (
                    await session.scalars(
                        select(EvidenceLedger).where(
                            EvidenceLedger.run_id == str(run.id),
                            EvidenceLedger.evidence_type == "MUTEKI_NATIVE_HTTP",
                        )
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(AgentTask).where(
                            AgentTask.run_id == str(run.id),
                            AgentTask.context_json["projection"].as_string()
                            == "structured_http",
                        )
                    )
                ).all()
            )
            task_results = list(
                (
                    await session.scalars(
                        select(AgentTaskResult).where(
                            AgentTaskResult.task_id == tasks[0].id,
                        )
                    )
                ).all()
            )
            verified_facts = list(
                (
                    await session.scalars(
                        select(VerifiedFact).where(VerifiedFact.run_id == str(run.id))
                    )
                ).all()
            )

            assert len(tool_calls) == 2
            assert len(artifacts) == 2
            assert len(observations) == 2
            assert len(evidence_rows) == 2
            assert len(tasks) == 1
            assert len(task_results) == 1
            assert verified_facts == []
            assert set(task_results[0].evidence_ids_json) == set(refs)

            artifacts_by_tool = {str(item.tool_call_id): item for item in artifacts}
            observations_by_tool = {str(item.tool_call_id): item for item in observations}
            evidence_by_tool = {str(item.tool_call_id): item for item in evidence_rows}
            task_id = str(tasks[0].id)
            for tool_call in tool_calls:
                tool_call_id = str(tool_call.id)
                artifact = artifacts_by_tool[tool_call_id]
                observation = observations_by_tool[tool_call_id]
                evidence = evidence_by_tool[tool_call_id]

                assert artifact.tool_call_id == tool_call.id
                assert evidence.artifact_id == artifact.id
                assert evidence.tool_call_id == tool_call.id
                assert evidence.agent_task_id == task_id
                assert evidence.source_chain == [str(artifact.id), str(tool_call.id), task_id]
                assert "headers" not in observation.facts_json
                assert "body" not in observation.facts_json
                assert "secret-body-one" not in json.dumps(observation.facts_json)
                assert "secret-body-two" not in json.dumps(observation.facts_json)
                stored = (workspace / artifact.file_path).read_text(encoding="utf-8")
                assert "headers" in stored
                assert "body" in stored

            await session.refresh(run)
            assert run.tool_call_count == 2
            assert run.run_total_logical_tool_calls == 2
            assert run.run_total_agent_steps == 2

        await engine.dispose()

    asyncio.run(run_case())


def test_structured_http_projection_is_scoped_by_intent(tmp_path) -> None:
    async def run_case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        workspace = tmp_path / "workspace-intents"
        workspace.mkdir()
        native_artifact = workspace / ".muteki-artifacts" / "http-evidence.json"
        native_artifact.parent.mkdir()
        native_artifact.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "tool": "http_request",
                            "method": "GET",
                            "url": "http://target.test/shared",
                            "status_code": 200,
                            "final_url": "http://target.test/shared",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        async with factory() as session:
            run = SolveRun(challenge_id="challenge-intents", workspace_path=str(workspace))
            session.add(run)
            await session.flush()
            bridge = SqlAlchemyOfficialEvidenceBridge(session, run, workspace)
            result = OfficialWorkerResult(
                True,
                "COMPLETED",
                "codex",
                evidence_artifact_path=str(native_artifact),
            )

            first = await bridge.ingest(
                run_id=str(run.id),
                worker_id="worker-native",
                intent_id="intent-a",
                result=result,
            )
            second = await bridge.ingest(
                run_id=str(run.id),
                worker_id="worker-native",
                intent_id="intent-b",
                result=result,
            )

            assert first != second
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.run_id == str(run.id),
                            Artifact.artifact_type == "muteki_native_http_record",
                        )
                    )
                ).all()
            )
            assert len(artifacts) == 2
            assert {str(item.file_path).split("/")[3] for item in artifacts} == {
                "intent-a",
                "intent-b",
            }

        await engine.dispose()

    asyncio.run(run_case())


def test_native_worker_backend_rejects_compatibility_graph(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "current.sqlite", challenge_id="run-current")
    pool = MutekiWorkerPool(graph, lambda job: None, max_workers=1)

    try:
        with pytest.raises(ValueError, match="APP_MUTEKI_GRAPH_BACKEND=upstream"):
            MutekiCoordinator(
                graph,
                MutekiReason(),
                pool,
                [EngineProfile("codex")],
                config={"worker_backend": "upstream_local"},
            )
    finally:
        graph.close()


def test_native_orchestrator_defaults_to_codex_and_forwards_account_root(tmp_path) -> None:
    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
    from app.solver.muteki.core.orchestrator import MutekiOrchestrator

    challenge = SimpleNamespace(
        id="native-config-challenge",
        name="Native config challenge",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://127.0.0.1.invalid",
        flag_pattern=r"flag\\{[^}]+\\}",
    )
    graph = UpstreamRuntimeGraph(
        tmp_path / "upstream.sqlite",
        challenge=challenge,
        challenge_id="native-config-run",
    )
    try:
        orchestrator = MutekiOrchestrator(
            graph,
            MutekiReason(),
            worker_runner=lambda _job: None,
            worker_backend="upstream_local",
            official_account_root="accounts",
        )
        assert orchestrator.engines[0].engine_id == "codex"
        assert orchestrator.coordinator.config.official_account_root == "accounts"
    finally:
        graph.close()
