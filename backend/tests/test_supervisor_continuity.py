import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import RunAttempt, RunEvent, RunExecutionLease, RunUserInput, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.schemas.multi_agent import FailureClassification
from app.services.failure_classification import normalize_failure_classification
from app.services.run_diagnostics import run_diagnostics_service
from app.services.run_finalizer import run_finalizer
from app.services.run_supervisor import RunSupervisor, run_supervisor
from app.services.stage_decider import stage_decider
from app.services.supervisor_progress import supervisor_progress_evaluator
from app.services.tool_failure_policy import blocked_failure_for_action, tool_failure_fingerprint
from app.services.user_input_consumer import consume_user_inputs


def test_failure_classification_normalizes_pydantic_object() -> None:
    value = FailureClassification(
        fingerprint="fp",
        classification="MYSQL_METADATA_EMPTY_RESULT",
    )
    assert normalize_failure_classification(value)["classification"] == "MYSQL_METADATA_EMPTY_RESULT"


def test_failure_classification_normalizes_legacy_object() -> None:
    class LegacyClassification:
        def __init__(self):
            self.classification = "TOOL_FAILURE"

    assert normalize_failure_classification(LegacyClassification())["classification"] == "TOOL_FAILURE"


def test_stage_decider_ignores_stale_phase_and_requires_metadata() -> None:
    decision = stage_decider.decide(
        asset_warranty_mysql=True,
        verified_fact_keys={
            "asset_warranty.valid_baseline",
            "asset_warranty.invalid_baseline",
            "asset_warranty.mysql_boolean_oracle",
            "asset_warranty.mysql_dbms",
            "asset_warranty.oracle_calibration_matrix",
        },
    )
    assert decision.stage == "MYSQL_METADATA_DISCOVERY"
    assert decision.details["stage"] == "database"


def test_tool_failure_fingerprint_is_stable_and_stage_sensitive() -> None:
    first = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "version", "VERSION()", "args-digest")
    same = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "version", "VERSION()", "args-digest")
    different_stage = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "tables", "VERSION()", "args-digest")
    assert first == same
    assert first != different_stage


def test_tool_failure_circuit_blocks_the_third_identical_action() -> None:
    fingerprint = tool_failure_fingerprint("mysql_metadata_discovery", "MYSQL_METADATA_EMPTY_RESULT", "version", "VERSION()", "args-digest")
    run = SimpleNamespace(recovery_checkpoint_json={"tool_failure_counts": {fingerprint: {
        "fingerprint": fingerprint,
        "tool_name": "mysql_metadata_discovery",
        "stage": "version",
        "target_expression": "VERSION()",
        "compiled_arguments_digest": "args-digest",
        "count": 2,
    }}}, current_phase="MYSQL_METADATA_DISCOVERY")
    blocked = blocked_failure_for_action(run, "mysql_metadata_discovery", {"stage": "version", "target_expression": "VERSION()"}, "args-digest")
    assert blocked is not None
    assert blocked["count"] == 2


def test_progress_snapshot_counts_as_progress() -> None:
    checkpoint = {}
    decision = supervisor_progress_evaluator.observe(
        checkpoint,
        stage="MYSQL_METADATA_DISCOVERY",
        error_code=None,
        before_facts=set(),
        after_facts=set(),
        before_capabilities=set(),
        after_capabilities=set(),
        progress_snapshot_changed=True,
    )
    assert decision.new_fact_or_capability is True
    assert checkpoint["supervisor_counters"]["no_progress_count"] == 0


@pytest.mark.asyncio
async def test_consume_user_inputs_marks_all_queued_rows_and_updates_hints(tmp_path: Path, monkeypatch) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'inputs.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="input", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="WAITING_USER")
        session.add(run)
        await session.flush()
        state = SolverState(run_id=run.id)
        session.add(state)
        session.add_all([
            RunUserInput(run_id=run.id, revision=2, content="try alternative strategy"),
            RunUserInput(run_id=run.id, revision=1, content="continue metadata"),
        ])
        await session.commit()
        wakes = []

        async def fake_enqueue(run_id, *, reason):
            wakes.append((run_id, reason))

        monkeypatch.setattr(run_supervisor, "enqueue", fake_enqueue)
        consumed = await consume_user_inputs(session, run)
        rows = list((await session.scalars(select(RunUserInput).where(RunUserInput.run_id == run.id).order_by(RunUserInput.revision))).all())
        refreshed_state = await session.get(SolverState, state.id)
        assert [item["revision"] for item in consumed["user_inputs"]] == [1, 2]
        assert all(item.status == "CONSUMED" and item.consumed_at is not None for item in rows)
        assert run.hints_json["user_inputs"][0]["content"] == "continue metadata"
        assert refreshed_state.last_decision_card_json["user_inputs"]
        assert wakes == [(run.id, "USER_INPUT_CONSUMED")]
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())
        consumed_event = next(event for event in events if event.event_type == "user_input.consumed")
        assert consumed_event.payload_json["run_id"] == run.id
        assert consumed_event.payload_json["revision"] == [1, 2]
        run.status = "WAITING_USER"
        run.recovery_checkpoint_json = {"question": "continue?", "options": ["retry"]}
        session.add(RunUserInput(run_id=run.id, revision=3, content="resume with the saved hint"))
        await session.commit()
        supervisor = RunSupervisor()
        continued = []

        async def fake_continue(session_arg, run_id_arg, user_message=None):
            continued.append((run_id_arg, user_message))
            return "CONTINUED"

        supervisor.continue_until_terminal = fake_continue
        assert await supervisor.continue_after_user_input(session, run.id) == "CONTINUED"
        await session.refresh(run)
        assert run.status == "PLANNING"
        assert continued == [(run.id, "User supplemental input v3: resume with the saved hint")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_enqueue_wakes_user_input_worker() -> None:
    supervisor = RunSupervisor()
    called = []

    async def fake_continue(run_id: str):
        called.append(run_id)

    supervisor.run_after_user_input_background = fake_continue
    await supervisor.enqueue("run-1", reason="USER_INPUT_RECEIVED")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await supervisor.stop_worker()
    assert called == ["run-1"]


@pytest.mark.asyncio
async def test_supervisor_rejects_duplicate_run_lock() -> None:
    supervisor = RunSupervisor()
    lock = asyncio.Lock()
    await lock.acquire()
    supervisor.run_supervisor_lock["run-1"] = lock
    outcome = await supervisor.run_background("run-1")
    assert outcome.error_code == "RUN_ALREADY_RUNNING"
    lock.release()


@pytest.mark.asyncio
async def test_terminal_reconcile_removes_all_running_resources(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'terminal.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="terminal", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="COMPLETED_UNSOLVED", current_phase="REPORTING")
        session.add(run)
        await session.flush()
        now = datetime.now(UTC)
        attempt = RunAttempt(run_id=run.id, attempt_number=1, engine_type="openai_compatible", status="RUNNING", finished_at=now)
        task = AgentTask(run_id=run.id, agent_role="RECON", task_kind="RECON", objective="test", status="RUNNING")
        session.add_all([attempt, task])
        await session.flush()
        lease = RunExecutionLease(run_id=run.id, attempt_id=attempt.id, owner_instance_id="test", lease_token="terminal-lease", acquired_at=now, heartbeat_at=now, expires_at=now + timedelta(minutes=1))
        tool_call = ToolCall(run_id=run.id, tool_name="mysql_metadata_discovery", arguments_json={"stage": "version"}, status="STARTED")
        session.add_all([lease, tool_call])
        await session.commit()

        await run_finalizer.terminal_reconcile(session, run)
        await session.refresh(attempt)
        await session.refresh(task)
        await session.refresh(tool_call)
        assert attempt.status != "RUNNING"
        assert await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)) is None
        assert task.status == "INTERRUPTED"
        assert tool_call.status == "INTERRUPTED"
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())
        assert any(event.event_type == "run.lifecycle.cleaned" for event in events)
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_self_check_passes_after_terminal_cleanup(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'self-check.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="self-check", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(tmp_path),
            status="COMPLETED_UNSOLVED",
            current_phase="REPORTING",
            report_json={
                "confirmed_facts": [],
                "completed_stages": ["BUSINESS_BASELINE"],
                "failed_tools": ["mysql_metadata_discovery"],
                "user_inputs": [],
                "next_steps": ["fix runner"],
            },
        )
        session.add(run)
        await session.flush()
        session.add(SolverState(run_id=run.id, current_phase="REPORTING"))
        await session.commit()

        result = await run_diagnostics_service.system_self_check(session, run)

        assert result["status"] == "PASS"
        assert all(item["status"] == "PASS" for item in result["checks"])
        assert result["reasons"] == []
    await engine.dispose()


def test_a_crashed_lifecycle_does_not_leave_the_run_on_running(tmp_path, monkeypatch) -> None:
    """A Run whose lifecycle coroutine dies must not stay RUNNING.

    Measured 2026-10-19 on an arena Run: ``MissingGreenlet`` escaped
    ``_run_muteki`` and ended ``run_background``.  The Run row stayed RUNNING,
    and because ``_active_run_tasks`` had already been unregistered in the
    ``finally`` block, nothing could reach it any more -- not even the cancel
    endpoint.  The lifecycle coroutine therefore has to close the Run itself.
    """

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'crash.db'}", poolclass=StaticPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            challenge = Challenge(
                name="crash", target_url="http://target.test", allowed_hosts=["target.test"]
            )
            session.add(challenge)
            await session.flush()
            run = SolveRun(
                challenge_id=challenge.id, workspace_path=str(tmp_path), status="RUNNING"
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        # ``run_background`` imports SessionLocal inside the method, so the
        # module attribute is what has to point at this test's engine.
        monkeypatch.setattr("app.core.database.SessionLocal", sessions)

        supervisor = RunSupervisor()

        async def explode(*_args, **_kwargs):
            raise RuntimeError("session died under the runtime")

        monkeypatch.setattr(supervisor, "continue_until_terminal", explode)

        outcome = await supervisor.run_background(run_id)

        assert outcome.status == "FAILED_ENGINE"
        assert outcome.error_code == "SUPERVISOR_CRASHED"
        async with sessions() as session:
            reloaded = await session.get(SolveRun, run_id)
            assert str(reloaded.status) == "FAILED_ENGINE"
            assert reloaded.last_error_code == "SUPERVISOR_CRASHED"
            assert "session died under the runtime" in (reloaded.last_error_message or "")

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_a_crash_while_waiting_for_the_user_keeps_the_run_answerable(tmp_path, monkeypatch) -> None:
    """WAITING_USER must survive a crash of the continuation task.

    It is the one non-terminal status where a human still owns the Run and can
    answer the pending question.  Scoring it FAILED_ENGINE would destroy the
    operator's only way back in, so the crash is recorded and the entry point
    is left open.
    """

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'waiting.db'}", poolclass=StaticPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            challenge = Challenge(
                name="waiting", target_url="http://target.test", allowed_hosts=["target.test"]
            )
            session.add(challenge)
            await session.flush()
            run = SolveRun(
                challenge_id=challenge.id,
                workspace_path=str(tmp_path),
                status="WAITING_USER",
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        monkeypatch.setattr("app.core.database.SessionLocal", sessions)

        supervisor = RunSupervisor()

        async def explode(*_args, **_kwargs):
            raise RuntimeError("continuation died")

        monkeypatch.setattr(supervisor, "continue_after_user_input", explode)

        outcome = await supervisor.run_after_user_input_background(run_id)

        assert outcome.status == "WAITING_USER"
        async with sessions() as session:
            reloaded = await session.get(SolveRun, run_id)
            assert str(reloaded.status) == "WAITING_USER"

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_the_user_input_continuation_also_closes_the_run_it_crashed_on(
    tmp_path, monkeypatch
) -> None:
    """The user-input path owns the Run for exactly as long as it lives.

    It is the second entry point with the same shape as ``run_background``:
    a task that unregisters itself in ``finally``.  An escape from either one
    leaves a row no endpoint can reach, so both need the same closing.
    """

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'waiting2.db'}", poolclass=StaticPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            challenge = Challenge(
                name="waiting2", target_url="http://target.test", allowed_hosts=["target.test"]
            )
            session.add(challenge)
            await session.flush()
            run = SolveRun(
                challenge_id=challenge.id, workspace_path=str(tmp_path), status="RUNNING"
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        monkeypatch.setattr("app.core.database.SessionLocal", sessions)

        supervisor = RunSupervisor()

        async def explode(*_args, **_kwargs):
            raise RuntimeError("continuation died")

        monkeypatch.setattr(supervisor, "continue_after_user_input", explode)

        outcome = await supervisor.run_after_user_input_background(run_id)

        assert outcome.status == "FAILED_ENGINE"
        assert outcome.error_code == "SUPERVISOR_CRASHED"
        async with sessions() as session:
            reloaded = await session.get(SolveRun, run_id)
            assert str(reloaded.status) == "FAILED_ENGINE"
            assert reloaded.last_error_code == "SUPERVISOR_CRASHED"

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_the_end_of_a_muteki_session_reaps_a_sandbox_that_outlived_finalize(monkeypatch) -> None:
    """A Run returning to the supervisor must not leave a container behind.

    Measured 2026-10-19: Run ``59de9927`` reached COMPLETED_SOLVED at 16:42:22
    while its container had been started at 16:42:21 -- after the Coordinator's
    Finalize teardown -- and sat idle with only ``runtime_agent`` inside.  No
    owner was left to reap it, so it survived until the next service restart.

    The listing is consulted first so an ordinary Run pays one ``docker ps -a``
    instead of a teardown call, and an *unknown* listing must fall through to
    asking: "could not list" is not "nothing is there".
    """

    calls: list[str] = []
    import muteki.solver.container_exec as container_exec
    from app.services import run_supervisor as module

    monkeypatch.setattr(
        container_exec, "teardown_container", lambda *_a, **_k: calls.append("teardown")
    )
    # This case is about the immediate pass; the late window is covered below.
    monkeypatch.setattr(module, "_LATE_REAP_DELAYS", ())

    async def scenario() -> None:
        monkeypatch.setattr(module, "_runs_owning_a_container", lambda _ids: ["run-1"])
        await module._reap_muteki_container("run-1")
        assert calls == ["teardown"], "a container that exists must be torn down"

        calls.clear()
        monkeypatch.setattr(module, "_runs_owning_a_container", lambda _ids: [])
        await module._reap_muteki_container("run-2")
        assert calls == [], "no container means no docker teardown call"

        calls.clear()
        monkeypatch.setattr(module, "_runs_owning_a_container", lambda _ids: None)
        await module._reap_muteki_container("run-3")
        assert calls == ["teardown"], "an unknown listing must not be read as empty"

    asyncio.run(scenario())


def test_a_sandbox_provisioned_after_the_run_finished_is_still_reaped(monkeypatch) -> None:
    """The teardown at Run end is not late enough on its own.

    Measured 2026-10-20 on the UNSOLVED path: Run ``eaf1de66`` reached
    COMPLETED_UNSOLVED at 00:00:45 and its Sandbox was started at 00:00:48.7 --
    three seconds after the only teardown that will ever know its name.  It then
    idled until someone noticed.  Provisioning can also start from the A-lane
    ``python_run`` channel, which never passes the official Worker's ``_stopped``
    guard, so the late arrival cannot be prevented at the source either.
    """

    calls: list[str] = []
    import muteki.solver.container_exec as container_exec
    from app.services import run_supervisor as module

    monkeypatch.setattr(
        container_exec, "teardown_container", lambda *_a, **_k: calls.append("teardown")
    )
    monkeypatch.setattr(module, "_LATE_REAP_DELAYS", (0.0, 0.0))

    listings = iter([[], ["run-9"]])

    async def scenario() -> None:
        monkeypatch.setattr(module, "_runs_owning_a_container", lambda _ids: next(listings))
        await module._reap_muteki_container("run-9")
        # Gone at Run end -- the immediate pass correctly does nothing.
        assert calls == []
        # ...and appears while the late window is still open.
        pending = list(module._late_reap_tasks)
        assert pending, "the late window must be scheduled"
        for task in pending:
            await task
        assert calls == ["teardown"]

    asyncio.run(scenario())
