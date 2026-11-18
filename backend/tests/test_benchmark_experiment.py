import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.benchmark.case_definition import BenchmarkCase
from app.benchmark.experiment import (
    ARMS,
    ExecutionResult,
    ExperimentConfigurationError,
    ExperimentError,
    ExperimentOutputExistsError,
    ExperimentRequest,
    ExperimentRunner,
    RealRunExecutor,
    _persist_evaluation_report,
    build_summary,
    parse_arms,
)


def _case(case_id: str, vulnerability_type: str = "sql-injection") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        vulnerability_type=vulnerability_type,
        target="http://example.test",
        initial_knowledge=["A search parameter is present."],
        expected_validation={"status": "VALIDATED"},
    )


def _write_case(case_dir: Path, case: BenchmarkCase) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{case.case_id}.json").write_text(
        case.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _report(success: bool) -> dict:
    return {
        "final_result": "SUCCESS" if success else "INCOMPLETE",
        "metrics": {
            "efficiency": {
                "duration_seconds": 1.5,
                "tool_calls": 3,
                "total_tokens": 20,
            }
        },
    }


class FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, int]] = []
        self.environment_modes: list[str | None] = []

    async def execute(self, request: ExperimentRequest) -> ExecutionResult:
        self.requests.append((request.case_id, request.arm, request.repeat))
        self.environment_modes.append(os.environ.get("C7F_KNOWLEDGE_MODE"))
        success = request.arm == "on" and request.repeat % 2 == 1
        return ExecutionResult(
            run_id=f"run-{request.case_id}-{request.arm}-{request.repeat}",
            status="COMPLETED_SOLVED" if success else "COMPLETED_UNSOLVED",
            evaluation_report=_report(success),
        )


@pytest.mark.asyncio
async def test_runner_executes_case_arm_repeat_and_writes_manifest_raw_and_summary(
    tmp_path,
):
    case_dir = tmp_path / "cases"
    output_dir = tmp_path / "output"
    _write_case(case_dir, _case("case-sqli"))
    _write_case(case_dir, _case("case-xss", "xss"))
    executor = FakeExecutor()
    runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=executor,
        arms=ARMS,
        repeats=2,
    )

    summary = await runner.run()

    assert len(executor.requests) == 8
    assert {arm for _, arm, _ in executor.requests} == {"off", "on"}
    assert {repeat for _, _, repeat in executor.requests} == {1, 2}
    assert len(set(executor.environment_modes)) == 1
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "plan.json").is_file()
    assert (output_dir / "summary.json").is_file()
    assert len(list((output_dir / "artifacts").glob("*.json"))) == 8
    assert summary["record_count"] == 8
    assert summary["by_arm"]["off"]["attempts"] == 4
    assert summary["by_arm"]["on"]["success"] == 2
    assert summary["paired_effect"]["sample_count"] == 4
    assert summary["paired_effect"]["success_delta"] == 0.5
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["immutable"] is True
    assert {entry["case_id"] for entry in manifest["cases"]} == {
        "case-sqli",
        "case-xss",
    }


@pytest.mark.asyncio
async def test_runner_resume_reuses_complete_artifacts_and_rejects_config_changes(tmp_path):
    case_dir = tmp_path / "cases"
    output_dir = tmp_path / "output"
    _write_case(case_dir, _case("case-sqli"))
    first_executor = FakeExecutor()
    first_runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=first_executor,
        arms=ARMS,
        repeats=2,
    )
    await first_runner.run()

    resume_executor = FakeExecutor()
    resume_runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=resume_executor,
        arms=ARMS,
        repeats=2,
        resume=True,
    )
    resumed = await resume_runner.run()

    assert resume_executor.requests == []
    assert resumed["record_count"] == 4

    non_resume_executor = FakeExecutor()
    non_resume_runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=non_resume_executor,
        arms=ARMS,
        repeats=2,
    )
    with pytest.raises(ExperimentOutputExistsError):
        await non_resume_runner.run()

    changed_runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=FakeExecutor(),
        arms=ARMS,
        repeats=2,
        resume=True,
        runtime_selection={"changed": True},
    )
    with pytest.raises(ExperimentConfigurationError):
        await changed_runner.run()


@pytest.mark.asyncio
async def test_runner_recovers_stale_lock_and_resumes(tmp_path, monkeypatch):
    import app.benchmark.experiment as experiment_module

    case_dir = tmp_path / "cases"
    output_dir = tmp_path / "output"
    _write_case(case_dir, _case("case-sqli"))
    await ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=FakeExecutor(),
        arms=ARMS,
        repeats=1,
    ).run()

    lock_path = output_dir.parent / f".{output_dir.name}.knowledge-experiment.lock"
    lock_path.write_text(
        f"pid=999999999\nhostname={experiment_module.socket.gethostname()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(experiment_module, "_process_is_running", lambda _pid: False)

    executor = FakeExecutor()
    summary = await ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=executor,
        arms=ARMS,
        repeats=1,
        resume=True,
    ).run()

    assert executor.requests == []
    assert summary["record_count"] == 2
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_runner_rejects_lock_owned_by_live_process(tmp_path, monkeypatch):
    import app.benchmark.experiment as experiment_module

    case_dir = tmp_path / "cases"
    output_dir = tmp_path / "output"
    _write_case(case_dir, _case("case-sqli"))
    lock_path = output_dir.parent / f".{output_dir.name}.knowledge-experiment.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        f"pid=1234\nhostname={experiment_module.socket.gethostname()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(experiment_module, "_process_is_running", lambda _pid: True)

    runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=output_dir,
        executor=FakeExecutor(),
        arms=ARMS,
        repeats=1,
    )
    with pytest.raises(ExperimentError, match="already owned"):
        await runner.run()
    assert lock_path.exists()


def test_summary_zero_sample_is_descriptive_only():
    summary = build_summary([])

    assert summary["record_count"] == 0
    assert summary["by_arm"]["off"]["attempts"] == 0
    assert summary["by_arm"]["off"]["success_rate"] is None
    assert summary["paired_effect"]["sample_count"] == 0
    assert summary["paired_effect"]["sample_adequate"] is False
    assert summary["paired_effect"]["claim"] == "descriptive_only"
    assert summary["paired_effect"]["warning"]


def test_summary_pairs_on_and_off_for_same_case_and_repeat():
    records = []
    for repeat in range(1, 11):
        off_success = repeat <= 4
        on_success = repeat <= 8
        for arm, success in (("off", off_success), ("on", on_success)):
            records.append(
                {
                    "case_id": "case-sqli",
                    "arm": arm,
                    "repeat": repeat,
                    "status": "COMPLETED",
                    "evaluation_report": _report(success),
                    "success": success,
                    "error": None,
                    "duration_seconds": 1.0,
                    "tool_calls": 2,
                    "total_tokens": 10,
                }
            )

    paired = build_summary(records)["paired_effect"]

    assert paired["sample_count"] == 10
    assert paired["sample_adequate"] is True
    assert paired["success_delta"] == 0.4
    assert paired["by_case"]["case-sqli"]["sample_count"] == 10
    assert paired["warning"] is None


def test_parse_arms_rejects_duplicates_and_unknown_values():
    assert parse_arms("on,off") == ("off", "on")
    with pytest.raises(ValueError, match="Duplicate arms"):
        parse_arms("off,off")
    with pytest.raises(ValueError, match="Unsupported arms"):
        parse_arms("off,hybrid")


@pytest.mark.asyncio
async def test_evaluation_report_persists_without_overwriting_solver_report():
    class FakeSession:
        def __init__(self):
            self.commit_count = 0

        async def commit(self):
            self.commit_count += 1

    session = FakeSession()
    run = SimpleNamespace(report_json={"generated": True, "flag_verified": True})
    report = {
        "run_id": "run-1",
        "final_result": "SUCCESS",
        "evaluator": {"name": "c7f-benchmark-evaluator"},
    }

    await _persist_evaluation_report(session, run, report)

    assert run.report_json == {
        "generated": True,
        "flag_verified": True,
        "evaluation_report": report,
    }
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_real_run_executor_binds_arm_to_each_run_without_global_environment(
    tmp_path,
    monkeypatch,
):
    import app.benchmark.experiment as experiment_module
    import app.services.methodology_hints as methodology_hints
    import app.services.solver_state as solver_state_module
    import app.services.workspace as workspace_module

    class FakeRun:
        def __init__(self, **kwargs):
            self.id = f"run-{kwargs['hints_json']['knowledge_mode']}"
            self.hints_json = dict(kwargs["hints_json"])
            self.workspace_path = kwargs["workspace_path"]

    class FakeSession:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    async def fake_initialize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        methodology_hints,
        "hints_for_challenge",
        lambda _challenge: {"hints": ["base"]},
    )
    monkeypatch.setattr(
        solver_state_module.solver_state_service,
        "initialize",
        fake_initialize,
    )
    monkeypatch.setattr(
        workspace_module,
        "create_workspace",
        lambda run_id, _challenge, _uploads: tmp_path / run_id,
    )
    monkeypatch.setattr(experiment_module, "_solve_run_class", lambda: FakeRun)

    executor = RealRunExecutor()
    challenge = SimpleNamespace(
        id="challenge-1",
        name="benchmark:case-sqli",
        description="fixture",
        challenge_type="WEB_TARGET",
        target_url="http://example.test",
        allowed_hosts=["example.test"],
    )
    worker = SimpleNamespace(id="worker-1", provider_type="openai_compatible")
    case = _case("case-sqli")
    original_mode = os.environ.get("C7F_KNOWLEDGE_MODE")

    for arm in ARMS:
        session = FakeSession()
        run = await executor._create_run(
            session,
            challenge=challenge,
            worker=worker,
            reason=None,
            request=ExperimentRequest(
                case=case,
                arm=arm,
                repeat=1,
                output_dir=tmp_path,
            ),
        )
        assert run.hints_json["knowledge_mode"] == arm
        assert session.added == [run]

    assert os.environ.get("C7F_KNOWLEDGE_MODE") == original_mode
