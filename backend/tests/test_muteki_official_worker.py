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


def test_official_worker_health_accepts_probe_answer_after_sighup(
    tmp_path, monkeypatch
) -> None:
    """An ``OK`` already on stdout outranks a ``rc=-1`` teardown signal.

    Measured 2026-10-26: 5/5 probe failures in one backend cycle were
    ``rc=-1 timed_out=False stdout='OK'`` -- the engine answered and was hung up
    during teardown.  Dropping the lane there costs the whole Run, so the answer
    wins.
    """

    class Handle:
        run_id = "health-cli-hangup"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        return SimpleNamespace(
            text="OK",
            raw_stderr="",
            timed_out=False,
            oom_killed=False,
            cancelled=False,
            runtime_status={"status": "finished", "rc": -1},
        )

    monkeypatch.setattr("muteki.solver.container_exec.run_cli_container", fake_run_cli_container)
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))
    adapter._container = Handle()

    healthy, reason = asyncio.run(
        adapter.health(EngineProfile("codex"), workspace=str(tmp_path))
    )

    assert healthy is True
    assert reason == ""


def test_official_worker_health_ignores_codex_stdin_notice(
    tmp_path, monkeypatch
) -> None:
    """codex's stdin notice is transport chatter, not a failed probe.

    Measured 2026-10-28, after the relay balance was restored: every codex probe
    failure was ``rc=-1 timed_out=False stdout='OK'
    stderr='Reading additional input from stdin...'``.  The engine had answered;
    only the teardown was odd.  Treating that as unhealthy drops the codex lane
    on every Run, so the notice is filtered and the answer still wins.
    """

    class Handle:
        run_id = "health-cli-codex-stdin"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        return SimpleNamespace(
            text="OK",
            raw_stderr="Reading additional input from stdin...\n",
            timed_out=False,
            oom_killed=False,
            cancelled=False,
            runtime_status={"status": "finished", "rc": -1},
        )

    monkeypatch.setattr("muteki.solver.container_exec.run_cli_container", fake_run_cli_container)
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))
    adapter._container = Handle()

    healthy, reason = asyncio.run(
        adapter.health(EngineProfile("codex"), workspace=str(tmp_path))
    )

    assert healthy is True
    assert reason == ""


def test_official_worker_health_rejects_sighup_without_clean_answer(
    tmp_path, monkeypatch
) -> None:
    """Only ``rc=-1`` qualifies, and only with an exact, stderr-free answer.

    A different non-zero code still means a broken launch, and any stderr output
    means the CLI complained after answering -- both stay unhealthy.
    """

    class Handle:
        run_id = "health-cli-hangup-dirty"

    def fake_run_cli_container(driver, argv, *, handle, cwd, timeout, env):
        return SimpleNamespace(
            text="OK",
            raw_stderr="error: upstream refused",
            timed_out=False,
            oom_killed=False,
            cancelled=False,
            runtime_status={"status": "finished", "rc": -1},
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


def test_official_worker_adapter_projects_redacted_step_output(tmp_path, monkeypatch) -> None:
    """A tool_result step now carries its output, but only in redacted form.

    The old contract dropped ``raw`` entirely, which made every post-mortem a
    guess: the trace said a tool ran and never what it printed.  The command and
    output are now forwarded -- with secrets masked and size bounded -- so this
    test pins both halves of that trade-off.
    """

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
                kind="tool",
                tool="shell",
                session="session-1",
                text="curl -H 'Authorization: Bearer abcdefghijklmnop' http://t/",
                call_id="call-1",
            )
        )
        kwargs["on_step"](
            SimpleNamespace(
                kind="tool_result",
                tool="shell",
                session="session-1",
                raw="secret response password=hunter2",
                call_id="call-1",
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
    assert len(steps) == 2

    invoke, outcome = steps
    # 1. The command is recorded, so the round is replayable...
    assert invoke["command"].startswith("curl ")
    # ...but the bearer token never reaches the durable stream.
    assert "abcdefghijklmnop" not in invoke["command"]
    assert "[redacted]" in invoke["command"]

    # 2. The result carries its output, paired with the command that produced it.
    assert outcome["command"].startswith("curl ")
    assert "secret response" in outcome["result_excerpt"]
    assert "hunter2" not in outcome["result_excerpt"]
    # The unbounded ``raw`` field itself is never projected.
    assert "raw" not in outcome


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


def test_official_worker_never_reprovisions_a_sandbox_after_teardown(monkeypatch) -> None:
    """A torn-down Sandbox must not be started again.

    Measured 2026-10-19: a Run reached COMPLETED_SOLVED while its container
    was started *after* the Finalize teardown and then sat idle with only
    ``runtime_agent`` inside -- nothing was left to reap it, so it leaked
    until the next service restart.  Whatever the late caller is, an adapter
    that has already stopped is not allowed to provision again.
    """

    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))
    started: list[str] = []

    def fake_ensure_container(*_args, **_kwargs):
        started.append("ensure")
        return SimpleNamespace(account_root=None)

    import app.solver.muteki.worker.official_worker as module

    monkeypatch.setattr(module, "ensure_container", fake_ensure_container, raising=False)
    import muteki.solver.container_exec as container_exec

    monkeypatch.setattr(container_exec, "ensure_container", fake_ensure_container)

    asyncio.run(adapter.stop())
    asyncio.run(adapter.start(run_id="run-1", workspace="/tmp/ws"))

    assert started == []
    assert adapter._container is None


def test_a_sandbox_provisioned_after_teardown_is_removed_again(monkeypatch) -> None:
    """Provisioning that finishes after Finalize must undo itself.

    ``ensure_container`` can take minutes (image pull, account projection),
    which is longer than a Run that solves quickly.  Finalize's teardown can
    therefore land *before* the Sandbox exists, and the container then appears
    with nobody left to reap it -- measured 2026-10-19: ``muteki-run-6bcb7b31``
    started in the same second its Run went COMPLETED_SOLVED and idled with
    only ``runtime_agent`` inside.
    """

    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="container"))
    torn_down: list[str] = []
    import muteki.solver.container_exec as container_exec

    monkeypatch.setattr(
        container_exec,
        "teardown_container",
        lambda run_id, **_kwargs: torn_down.append(str(run_id)),
    )

    def slow_ensure_container(*_args, **_kwargs):
        # Finalize's teardown lands while provisioning is still in flight.
        adapter._stopped = True
        return SimpleNamespace(account_root=None)

    monkeypatch.setattr(container_exec, "ensure_container", slow_ensure_container)

    asyncio.run(adapter.start(run_id="run-1", workspace="/tmp/ws"))

    assert torn_down == ["run-1"]
    assert adapter._container is None


# ── the containerised Claude root guard (2026-10-25) ────────────────────────


def test_the_claude_probe_gets_the_root_guard_escape_hatch() -> None:
    """Measured: every containerised Claude Run lost its lane to this.

    The worker image is ``uid=0``.  Claude CLI 2.1.98 refuses
    ``--dangerously-skip-permissions`` under root and prints

        --dangerously-skip-permissions cannot be used with root/sudo privileges

    before any network call, so the PREPARE probe failed on every Run
    (``muteki.prepare.engine.checked`` -> ``WORKER_HEALTHCHECK_FAILED``), the
    Coordinator logged ``ENGINE_UNAVAILABLE``, and the Run ran a single Codex
    lane.  The relay was reachable the whole time -- the host-side config test
    answered in 3.2s.

    ``IS_SANDBOX=1`` is the CLI's own opt-out.  It has to reach the process, so
    it has to survive ``_safe_environment``, whose prefix filter would otherwise
    drop it and produce exactly the same symptom as never setting it.
    """

    from app.solver.muteki.worker.official_worker import (
        _claude_root_permission_env,
        _safe_environment,
    )

    # The filter must not eat it: a dropped variable is indistinguishable from
    # an unset one at the CLI.
    kept = _safe_environment({"IS_SANDBOX": "1", "ANTHROPIC_API_KEY": "k", "UNRELATED": "x"})
    assert kept["IS_SANDBOX"] == "1"
    assert kept["ANTHROPIC_API_KEY"] == "k"
    assert "UNRELATED" not in kept

    environment: dict[str, str] = {}
    _claude_root_permission_env("claude", environment)
    assert environment["IS_SANDBOX"] == "1"


def test_the_root_guard_is_only_set_for_claude() -> None:
    """Codex has no such guard; setting it there would be noise in the env."""

    from app.solver.muteki.worker.official_worker import _claude_root_permission_env

    for engine in ("codex", "openai_compatible", "cursor"):
        environment: dict[str, str] = {}
        _claude_root_permission_env(engine, environment)
        assert environment == {}, engine


def test_an_operator_supplied_sandbox_flag_is_not_overwritten() -> None:
    """``setdefault``, not assignment: an explicit value wins.

    Someone debugging a non-container launch may need ``IS_SANDBOX=0``; forcing
    ``1`` would make the override silently ineffective, which is worse than the
    bug because it looks configured.
    """

    from app.solver.muteki.worker.official_worker import _claude_root_permission_env

    environment = {"IS_SANDBOX": "0"}
    _claude_root_permission_env("claude", environment)
    assert environment["IS_SANDBOX"] == "0"


def test_the_root_guard_recognises_the_engine_id_spelling() -> None:
    """The first version of this fix shipped inert; this is the test that catches it.

    ``OfficialWorkerAdapter.health`` passes ``profile.engine_id`` -- the value is
    ``claude-cli:7e58443e-b983-...``, not ``claude``.  An equality test against
    ``"claude"`` therefore never fired, ``IS_SANDBOX`` was never set, and the
    health check kept failing in exactly the way the fix was supposed to cure.

    The unit tests written alongside that fix all passed because they fed the
    helper ``"claude"`` -- the one spelling the production caller never uses.
    A test that only exercises the name you expect is not a test of the call
    site.
    """

    from app.solver.muteki.worker.official_worker import _claude_root_permission_env

    # Every spelling that has to work.
    for engine_id in (
        "claude-cli:7e58443e-b983-4876-b174-7b5358d6f77d",
        "claude-cli",
        "claude",
        "claude_code",
        "claude-code",
        "Claude-CLI:abc",  # case is not a signal
    ):
        environment: dict[str, str] = {}
        _claude_root_permission_env(engine_id, environment)
        assert environment.get("IS_SANDBOX") == "1", engine_id

    # And every spelling that must not.
    for other in (
        "codex-cli:7dc8b94a-802d-41b4-bb14-dfdd721a25ed",
        "codex",
        "cursor_cli",
        "openai_compatible",
        "claudeless",
        "",
    ):
        environment = {}
        _claude_root_permission_env(other, environment)
        assert environment == {}, other


# ── the dual-stack host alias (2026-10-26) ─────────────────────────────────


def test_the_ipv6_row_for_the_proxy_alias_is_removed() -> None:
    """Measured: one lane healthy and one dead, same container, same relay.

    ``ensure_container`` launches the Worker with
    ``--add-host host.docker.internal:host-gateway``.  On Docker Desktop that
    writes **two** rows::

        192.168.65.254      host.docker.internal
        fdc4:f303:9324::254 host.docker.internal

    Both proxy launchers bind ``0.0.0.0``, which is IPv4 only, so the alias
    resolves to an address nothing listens on.  ``getent hosts`` inside the
    container returned *only* the IPv6 row, which is the address Codex dials.

    curl and Node fall back to the other family; Codex is Rust/reqwest with
    Happy Eyeballs and does not, so every Codex probe died at
    ``WORKER_HEALTHCHECK_FAILED`` while Claude on the same container was
    healthy -- for days, while the relay was fine.
    """

    from app.solver.muteki.worker.official_worker import _PROXY_HOST_ALIAS, _pin_host_alias_to_ipv4

    assert _PROXY_HOST_ALIAS == "host.docker.internal"
    # A blank container name must not shell out.
    assert _pin_host_alias_to_ipv4("") is False
    assert _pin_host_alias_to_ipv4("   ") is False


def test_the_alias_repair_is_best_effort() -> None:
    """A repair that cannot run must not take the Run down with it.

    The alias is a routing nicety: when the container is already gone, when the
    image has no shell, or when ``/etc/hosts`` is not writable, the probe should
    proceed and report the engine's own verdict.  Raising here would convert a
    cosmetic fix-up into a lost Run.
    """

    from app.solver.muteki.worker.official_worker import _pin_host_alias_to_ipv4

    # No such container: docker exec fails, the helper reports False, no raise.
    assert _pin_host_alias_to_ipv4("muteki-nonexistent-container-for-test") is False


# ── 体检失败必须留下证据（2026-10-26）───────────────────────────────────────


def test_a_failed_probe_writes_its_real_output_to_the_log(caplog) -> None:
    """``reason_code`` 是枚举，证据得另找地方放下。

    为回答「codex 为什么体检失败」，2026-10-26 翻了几个小时的 ``run_events``：
    里面只有一个 ``WORKER_HEALTHCHECK_FAILED``，而容器在结论落下时已经销毁，
    stderr 跟着一起没了。原因码本身不能改（UI 与调度都读它），所以失败时留
    一条带 rc / stdout / stderr 的 warning 是唯一的出口。
    """

    import logging
    from types import SimpleNamespace

    from app.solver.muteki.worker.official_worker import _log_health_failure

    result = SimpleNamespace(
        runtime_status={"rc": 127},
        timed_out=False,
        text="",
        raw_stderr="sh: 1: codex: not found",
    )
    with caplog.at_level(logging.WARNING):
        reason = _log_health_failure("codex-cli:abc", "WORKER_HEALTHCHECK_FAILED", result)

    assert reason == "WORKER_HEALTHCHECK_FAILED"
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "codex: not found" in logged
    assert "rc=127" in logged


def test_a_healthy_probe_is_not_logged_as_a_failure(caplog) -> None:
    """正常轮次不该刷 warning：这条路径每个 Run 至少走两次。"""

    import logging
    from types import SimpleNamespace

    from app.solver.muteki.worker.official_worker import _log_health_failure

    with caplog.at_level(logging.WARNING):
        assert _log_health_failure("x", "HEALTHY", SimpleNamespace()) == "HEALTHY"

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_codex_oauth_home_is_dropped_when_an_api_key_is_present() -> None:
    """账号目录里的过期 OAuth 不能盖掉运行时注入的 key。

    ``runtime_env_for_engine`` 只按账号文件判断是否设置 ``CODEX_HOME``，
    看不到 runtime 已注入的 ``OPENAI_API_KEY``（本地 Responses 代理的 token）。
    codex 优先用 auth.json 并刷新过期 token，该刷新被地区限制拒绝，
    表现为探针已答 OK 仍以 rc=-1 收场 —— 即「引擎死了」。
    """

    from app.solver.muteki.worker.official_worker import _drop_stale_codex_oauth_home

    environment = {
        "OPENAI_API_KEY": "proxy-token",
        "CODEX_HOME": "/muteki/accounts/codex-main/codex-home",
    }

    _drop_stale_codex_oauth_home(environment)

    assert environment == {"OPENAI_API_KEY": "proxy-token"}


def test_codex_oauth_home_survives_when_no_api_key_is_provided() -> None:
    """真正靠 OAuth 登录的账号必须保留 CODEX_HOME。"""

    from app.solver.muteki.worker.official_worker import _drop_stale_codex_oauth_home

    environment = {"CODEX_HOME": "/muteki/accounts/codex-main/codex-home"}

    _drop_stale_codex_oauth_home(environment)

    assert environment["CODEX_HOME"].endswith("codex-home")


def test_container_handle_maps_to_posix_paths_regardless_of_host_platform() -> None:
    """The container is Linux: a backslash there is part of a FILE NAME.

    Measured 2026-10-29: every Worker received
    ``MUTEKI_BLACKBOARD_DB=/home/kali/workspace/graph\\upstream_shared_graph.db``,
    so sqlite3 created a shadow database next to the real one and
    ``blackboard.py`` answered ``no such table: events`` 404 times in one Run.
    """

    from muteki.solver.container_exec import CONTAINER_WORKSPACE, ContainerHandle

    handle = ContainerHandle(
        run_id="run-1",
        host_workspace=str(Path("C:/runs/run-1/muteki")),
        container="container-1",
    )

    mapped = handle.to_container_path(str(Path("C:/runs/run-1/muteki/graph/board.db")))

    assert mapped == f"{CONTAINER_WORKSPACE}/graph/board.db"
    assert "\\" not in mapped


def test_containerize_environment_fixes_separators_of_an_already_mapped_path() -> None:
    """A value that is already a container path is not automatically a good one.

    ``cli_solver._worker_env`` overwrites ``MUTEKI_BLACKBOARD_DB`` with
    ``to_container_path(...)``, so by the time the adapter sees it the value has
    the container ROOT but host separators.  Skipping it as "already container"
    is what let the broken path reach the Worker.
    """

    mapped = _containerize_environment(
        {
            "MUTEKI_WORKSPACE": "/home/kali/workspace",
            "MUTEKI_BLACKBOARD_DB": "/home/kali/workspace/graph\\upstream_shared_graph.db",
        },
        SimpleNamespace(to_container_path=lambda _: "/home/kali/workspace/wrong"),
    )

    assert mapped["MUTEKI_BLACKBOARD_DB"] == (
        "/home/kali/workspace/graph/upstream_shared_graph.db"
    )
    assert mapped["MUTEKI_WORKSPACE"] == "/home/kali/workspace"


def test_shell_emitted_fact_markers_reach_the_board() -> None:
    """A Worker's findings are printed by commands, not restated in its reply.

    Run ``4b65af58`` (Web6) echoed ``VERIFIED_FACT=`` four times in real command
    output and finished with ``fact_added = 0``: the solver parses markers out of
    the model's REPLY text, and its live tool-output parsing sits behind
    ``bus is not None``, which the arena never sets.

    The graph is the RAW official one (``add_evidence`` / ``add_dead_end(reason=)``)
    because that is what ``UpstreamRuntimeGraph.native_graph()`` hands the adapter
    in production -- NOT the ``add_fact`` facade above it.
    """

    written: list[tuple[str, str, bool, str | None]] = []
    dead_ends: list[tuple[str, str]] = []

    class OfficialGraph:
        def add_evidence(self, *, actor, source, fact, verified=False,
                         confidence=1.0, intent_id=None):
            written.append((actor, fact, verified, intent_id))
            return len(written)

        def add_dead_end(self, *, actor, reason):
            dead_ends.append((actor, reason))
            return len(dead_ends)

    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local"), shared_graph=OfficialGraph()
    )
    job = SimpleNamespace(worker_id="worker-1", intent_id="intent-1")

    adapter._on_step(
        job,
        SimpleNamespace(
            kind="tool_result",
            raw=(
                "VERIFIED_FACT=Flask app; GET /console returns the Werkzeug debugger\n"
                "DEADEND=POST /login rejects admin:admin with 403\n"
            ),
            text="",
            tool="shell",
            session="",
            call_id="call-1",
        ),
    )
    published = adapter._flush_tool_markers(job)

    assert published == 2
    assert written == [
        (
            "worker-1",
            "Flask app; GET /console returns the Werkzeug debugger",
            False,
            "intent-1",
        )
    ], "a printed marker is a claim, so it must not be published as verified"
    assert dead_ends == [("worker-1", "POST /login rejects admin:admin with 403")]
    # the buffer is consumed, so a second flush cannot double-post the board
    assert adapter._flush_tool_markers(job) == 0


def test_shell_markers_also_work_through_the_compatibility_facade() -> None:
    """Tests and plugins may hand the adapter the ``add_fact`` facade instead."""

    facts: list[str] = []

    class Facade:
        def add_fact(self, *, actor, content, verified=False):
            facts.append(content)
            return len(facts)

        def add_dead_end(self, *, actor, description):
            facts.append(f"DEADEND:{description}")
            return len(facts)

    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local"), shared_graph=Facade()
    )
    job = SimpleNamespace(worker_id="worker-1", intent_id="intent-1")

    adapter._on_step(
        job,
        SimpleNamespace(
            kind="tool_result",
            raw="VERIFIED_FACT=admin cookie is unsigned base64\nDEADEND=/backup returns 404\n",
            text="",
            tool="shell",
            session="",
            call_id="call-2",
        ),
    )

    assert adapter._flush_tool_markers(job) == 2
    assert facts == ["admin cookie is unsigned base64", "DEADEND:/backup returns 404"]


def test_a_marker_that_is_a_regex_is_not_an_observation() -> None:
    """Workers grep the transcript for their own markers (run-11550).

    That puts ``bl_|VERIFIED_FACT=.*L4`` in the output; it is a search pattern,
    not a finding, and the board must not gain a fact from it.
    """

    from app.solver.muteki.worker.official_worker import _tool_output_markers

    facts, dead_ends = _tool_output_markers(
        "VERIFIED_FACT=bl_|VERIFIED_FACT=.*L4\n"
        "I will print VERIFIED_FACT=x later\n"
        "VERIFIED_FACT=real one, endpoint /admin returns 200 for the guest cookie\n"
    )

    assert facts == ["real one, endpoint /admin returns 200 for the guest cookie"]
    assert dead_ends == []


def test_a_fact_reaches_the_board_while_the_worker_is_still_running() -> None:
    """Buffering until ``execute`` returns made every finding worthless.

    Measured 2026-10-29 on ``84e466d5``: all four collected facts carried the
    same timestamp as ``worker_finished``, 1.5 minutes after the last one was
    printed.  The whole point of the board is that minute 3's fact changes
    minute 4's plan; a fact that lands after the Worker exits only decorates the
    report.
    """

    facts: list[str] = []

    class Graph:
        def add_evidence(self, *, actor, source, fact, verified=False,
                         confidence=1.0, intent_id=None):
            facts.append(fact)

    class Loop:
        """Records what the step thread asked the loop to run."""

        def __init__(self) -> None:
            self.scheduled: list[tuple] = []

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback, *args) -> None:
            self.scheduled.append((callback, args))

    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local"), shared_graph=Graph()
    )
    job = SimpleNamespace(worker_id="worker-2", intent_id="intent-9")
    loop = Loop()
    adapter._job_loops[adapter._job_key(job)] = loop

    adapter._on_step(
        job,
        SimpleNamespace(
            kind="tool_result",
            raw="VERIFIED_FACT=debug console leaks SECRET_KEY\n",
            text="",
            tool="shell",
            session="",
            call_id="call-1",
        ),
    )

    assert len(loop.scheduled) == 1, "capturing a fact must schedule an immediate flush"
    callback, args = loop.scheduled[0]
    assert callback == adapter._flush_tool_markers
    callback(*args)
    assert facts == ["debug console leaks SECRET_KEY"]

    # Replaying the same line later must not restate the fact on the board.
    adapter._on_step(
        job,
        SimpleNamespace(
            kind="tool_result",
            raw="VERIFIED_FACT=debug console leaks SECRET_KEY\n",
            text="",
            tool="shell",
            session="",
            call_id="call-2",
        ),
    )
    assert loop.scheduled[-1][0] == adapter._flush_tool_markers
    loop.scheduled[-1][0](*loop.scheduled[-1][1])
    assert facts == ["debug console leaks SECRET_KEY"]


def test_the_final_flush_releases_the_per_job_bookkeeping() -> None:
    """A finished Worker must not leak its loop or its dedupe set.

    Both tables are keyed per job and a Run reuses worker ids across rounds, so a
    stale ``_job_loops`` entry would send the next round's markers to a closed
    loop and a stale dedupe set would silently drop a fact that is genuinely new.
    """

    facts: list[str] = []

    class Graph:
        def add_evidence(self, *, actor, source, fact, verified=False,
                         confidence=1.0, intent_id=None):
            facts.append(fact)

    adapter = OfficialWorkerAdapter(
        OfficialWorkerConfig(backend="local"), shared_graph=Graph()
    )
    job = SimpleNamespace(worker_id="worker-1", intent_id="intent-1")
    adapter._job_loops[adapter._job_key(job)] = SimpleNamespace(is_closed=lambda: True)
    adapter._published_markers[adapter._job_key(job)] = {"old fact"}

    adapter._capture_tool_markers(job, "VERIFIED_FACT=fresh fact\n")
    assert adapter._flush_tool_markers(job, final=True) == 1
    assert adapter._job_key(job) not in adapter._job_loops
    assert adapter._job_key(job) not in adapter._published_markers
    assert facts == ["fresh fact"]
