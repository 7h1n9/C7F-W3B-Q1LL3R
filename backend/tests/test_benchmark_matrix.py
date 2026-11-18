"""The matrix entry points: the CLI, the job service, and the launch API.

Nothing here runs a model.  The CLI's real executor is replaced by a stub, and
the service's ``Popen`` by a fake pid, so these tests pin the wiring — argument
validation, exit codes, state resolution — rather than the solver.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.api.v1.knowledge_experiments import MatrixLaunchRequest, launch_matrix, matrix_status
from app.benchmark import __main__ as matrix_cli
from app.benchmark.experiment import ExecutionResult
from app.benchmark.range import RangeEndpoint
from app.core.exceptions import DomainError
from app.services import benchmark_matrix

MINIMAL_CASE = {
    "case_id": "case-sqli",
    "vulnerability_type": "SQLI",
    "target": "http://{{host}}:{{port}}",
    "attack_surface": [{"endpoint": "/api/warranty/check", "parameter": "department"}],
}


def _case_dir(tmp_path: Path, **overrides) -> Path:
    directory = tmp_path / "cases"
    directory.mkdir(exist_ok=True)
    payload = {**MINIMAL_CASE, **overrides}
    (directory / f"{payload['case_id']}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return directory


class _StubExecutor:
    """Stands in for RealRunExecutor without touching models or the database."""

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result
        self.requests: list = []

    async def preflight(self) -> dict:
        return {"status": "ready", "worker_model": {"name": "stub"}}

    async def execute(self, request) -> ExecutionResult:
        self.requests.append(request)
        if self.result is not None:
            return self.result
        return ExecutionResult(
            run_id=f"run-{request.case_id}-{request.arm}-{request.repeat}",
            status="COMPLETED_SOLVED",
            evaluation_report={"final_result": "SUCCESS"},
        )


def _successful() -> ExecutionResult:
    return ExecutionResult(
        run_id="run-1",
        status="COMPLETED_SOLVED",
        evaluation_report={"final_result": "SUCCESS"},
    )


def _failed() -> ExecutionResult:
    return ExecutionResult(
        run_id="run-1",
        status="EXECUTION_ERROR",
        evaluation_report=None,
        error="boom",
    )


def _install(monkeypatch: pytest.MonkeyPatch, executor) -> None:
    monkeypatch.setattr(matrix_cli, "RealRunExecutor", lambda **_: executor)


# --------------------------------------------------------------------------- CLI


def test_dry_run_writes_a_plan_and_never_calls_the_model(tmp_path: Path, monkeypatch) -> None:
    executor = _StubExecutor()
    _install(monkeypatch, executor)
    output = tmp_path / "out"

    code = matrix_cli.main(
        [
            "--case-dir",
            str(_case_dir(tmp_path)),
            "--output-dir",
            str(output),
            "--repeats",
            "2",
            "--json",
            "--dry-run",
        ]
    )

    assert code == matrix_cli.EXIT_OK
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    # 1 case x 2 arms x 2 repeats
    assert len(plan["tasks"]) == 4
    assert executor.requests == []


def test_a_bad_repeat_count_is_a_configuration_error(tmp_path: Path) -> None:
    code = matrix_cli.main(
        ["--case-dir", str(_case_dir(tmp_path)), "--repeats", "0", "--dry-run"]
    )
    assert code == matrix_cli.EXIT_CONFIG


def test_a_missing_case_directory_is_a_configuration_error(tmp_path: Path) -> None:
    code = matrix_cli.main(["--case-dir", str(tmp_path / "nope"), "--dry-run"])
    assert code == matrix_cli.EXIT_CONFIG


def test_a_case_directory_without_cases_is_a_configuration_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code = matrix_cli.main(["--case-dir", str(empty), "--dry-run"])
    assert code == matrix_cli.EXIT_CONFIG


def test_an_unready_preflight_refuses_before_burning_a_run(tmp_path: Path, monkeypatch) -> None:
    class _NotReady(_StubExecutor):
        async def preflight(self) -> dict:
            return {"status": "error", "error": "MODEL_CONFIG_REQUIRED"}

    _install(monkeypatch, _NotReady())
    code = matrix_cli.main(
        ["--case-dir", str(_case_dir(tmp_path)), "--output-dir", str(tmp_path / "out")]
    )
    assert code == matrix_cli.EXIT_NOT_READY


def test_a_clean_matrix_exits_zero(tmp_path: Path, monkeypatch) -> None:
    executor = _StubExecutor(_successful())
    _install(monkeypatch, executor)
    output = tmp_path / "out"

    code = matrix_cli.main(
        ["--case-dir", str(_case_dir(tmp_path)), "--output-dir", str(output), "--repeats", "2"]
    )

    assert code == matrix_cli.EXIT_OK
    assert len(executor.requests) == 4
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["record_count"] == 4
    assert summary["by_arm"]["on"]["success_rate"] == 1.0
    # No metadata was supplied by the stub, so nothing was measurable.
    assert summary["by_arm"]["on"]["process_metrics"]["runs_measured"] == 0


def test_a_failing_task_exits_three(tmp_path: Path, monkeypatch) -> None:
    _install(monkeypatch, _StubExecutor(_failed()))
    code = matrix_cli.main(
        ["--case-dir", str(_case_dir(tmp_path)), "--output-dir", str(tmp_path / "out")]
    )
    assert code == matrix_cli.EXIT_TASK_FAILED


def test_resume_reuses_completed_artifacts(tmp_path: Path, monkeypatch) -> None:
    executor = _StubExecutor(_successful())
    _install(monkeypatch, executor)
    output = tmp_path / "out"
    arguments = ["--case-dir", str(_case_dir(tmp_path)), "--output-dir", str(output)]

    assert matrix_cli.main(arguments) == matrix_cli.EXIT_OK
    first = len(executor.requests)

    # A second run must not silently overwrite a finished matrix.
    assert matrix_cli.main(arguments) == matrix_cli.EXIT_CONFIG
    assert len(executor.requests) == first

    assert matrix_cli.main([*arguments, "--resume"]) == matrix_cli.EXIT_OK
    assert len(executor.requests) == first


def test_randomize_per_repeat_rotates_once_for_each_repetition(
    tmp_path: Path, monkeypatch
) -> None:
    _install(monkeypatch, _StubExecutor(_successful()))
    rotations: list[int] = []

    def _fake_bring_up(**_kwargs):
        rotations.append(len(rotations) + 1)
        return RangeEndpoint("127.0.0.1", 8000 + len(rotations)), "flag{stub}"

    monkeypatch.setattr(matrix_cli, "bring_up", _fake_bring_up)

    code = matrix_cli.main(
        [
            "--case-dir",
            str(_case_dir(tmp_path)),
            "--output-dir",
            str(tmp_path / "out"),
            "--repeats",
            "2",
            "--randomize-range",
            "repeat",
        ]
    )

    assert code == matrix_cli.EXIT_OK
    # One rotation per repetition, not one per task: 2 repeats -> 2.
    assert len(rotations) == 2


def test_randomize_once_per_matrix_rotates_a_single_time(tmp_path: Path, monkeypatch) -> None:
    _install(monkeypatch, _StubExecutor(_successful()))
    rotations: list[int] = []

    def _fake_bring_up(**_kwargs):
        rotations.append(len(rotations) + 1)
        return RangeEndpoint("127.0.0.1", 8001), "flag{stub}"

    monkeypatch.setattr(matrix_cli, "bring_up", _fake_bring_up)

    code = matrix_cli.main(
        [
            "--case-dir",
            str(_case_dir(tmp_path)),
            "--output-dir",
            str(tmp_path / "out"),
            "--repeats",
            "3",
            "--randomize-range",
            "run",
        ]
    )

    assert code == matrix_cli.EXIT_OK
    assert len(rotations) == 1


# --------------------------------------------------------------------------- service


@pytest.fixture()
def experiments_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the service at a temp root.

    ``PROJECT_ROOT`` is moved too: the case directory has to look like it lives
    inside the repository, and a temp dir never does.
    """

    root = tmp_path / "experiments"
    root.mkdir()
    monkeypatch.setattr(benchmark_matrix, "EXPERIMENTS_ROOT", root)
    monkeypatch.setattr(benchmark_matrix, "PROJECT_ROOT", tmp_path)
    return root


def _request(experiments_root: Path, **overrides) -> benchmark_matrix.MatrixRequest:
    payload = {
        "experiment_id": "matrix-a",
        "case_dir": str(_case_dir(experiments_root.parent)),
        "output_dir": str(experiments_root / "matrix-a"),
    }
    payload.update(overrides)
    return benchmark_matrix.build_request(payload)


def test_build_request_rejects_an_output_directory_outside_the_root(
    experiments_root: Path, tmp_path: Path
) -> None:
    with pytest.raises(DomainError) as caught:
        _request(experiments_root, output_dir=str(tmp_path / "elsewhere"))

    assert caught.value.code == "MATRIX_OUTPUT_DIR_NOT_ALLOWED"
    assert caught.value.status_code == 422


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"repeats": 0}, "MATRIX_REPEATS_OUT_OF_RANGE"),
        ({"repeats": 500}, "MATRIX_REPEATS_OUT_OF_RANGE"),
        ({"arms": "sideways"}, "MATRIX_ARMS_INVALID"),
        ({"randomize_range": "sometimes"}, "MATRIX_RANDOMIZE_SCOPE_INVALID"),
        ({"experiment_id": "../escape"}, "MATRIX_EXPERIMENT_ID_INVALID"),
    ],
)
def test_build_request_rejects_bad_arguments(
    experiments_root: Path, overrides: dict, code: str
) -> None:
    with pytest.raises(DomainError) as caught:
        _request(experiments_root, **overrides)
    assert caught.value.code == code


def test_build_request_rejects_an_out_of_tree_case_directory(experiments_root: Path) -> None:
    with pytest.raises(DomainError) as caught:
        _request(experiments_root, case_dir="C:/Windows/System32")
    assert caught.value.code == "MATRIX_CASE_DIR_NOT_ALLOWED"


def test_build_request_defaults_to_both_arms_and_no_randomisation(
    experiments_root: Path,
) -> None:
    request = _request(experiments_root)

    assert request.arms == ("off", "on")
    assert request.randomize_range == "never"
    assert request.repeats == 1
    assert "--json" in request.argv()


def test_start_refuses_a_non_empty_output_without_resume(
    experiments_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(experiments_root)
    request.output_dir.mkdir(parents=True)
    (request.output_dir / "leftover.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DomainError) as caught:
        benchmark_matrix.start(request)

    assert caught.value.code == "MATRIX_OUTPUT_DIR_NOT_EMPTY"
    assert caught.value.status_code == 409


def test_start_spawns_and_records_running_state(
    experiments_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeProcess:
        pid = 4242

    monkeypatch.setattr(benchmark_matrix.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(benchmark_matrix, "_process_is_running", lambda pid: True)

    state = benchmark_matrix.start(_request(experiments_root))

    assert state["status"] == benchmark_matrix.STATUS_RUNNING
    assert state["pid"] == 4242
    assert state["planned_tasks"] == 0
    written = benchmark_matrix.read_json(Path(state["output_dir"]) / "state.json")
    assert written["experiment_id"] == "matrix-a"
    assert (Path(state["output_dir"]) / "run_request.json").is_file()


def test_a_dead_pid_without_a_summary_is_recoverable(experiments_root: Path) -> None:
    directory = experiments_root / "matrix-dead"
    directory.mkdir()
    benchmark_matrix.write_json(
        directory / "state.json",
        {"experiment_id": "matrix-dead", "pid": 999_999, "output_dir": str(directory)},
    )
    state = benchmark_matrix.read_experiment("matrix-dead")

    assert state["status"] == benchmark_matrix.STATUS_INTERRUPTED
    assert "--resume" in (state["recover_with"] or "")


def test_a_summary_means_finished_even_if_the_pid_is_gone(experiments_root: Path) -> None:
    directory = experiments_root / "matrix-done"
    directory.mkdir()
    benchmark_matrix.write_json(
        directory / "state.json",
        {"experiment_id": "matrix-done", "pid": 999_999, "output_dir": str(directory)},
    )
    benchmark_matrix.write_json(directory / "summary.json", {"record_count": 4})

    state = benchmark_matrix.read_experiment("matrix-done")

    assert state["status"] == benchmark_matrix.STATUS_FINISHED
    assert state["recover_with"] is None
    assert state["summary"] == {"record_count": 4}


def test_an_unknown_experiment_is_a_404(experiments_root: Path) -> None:
    with pytest.raises(DomainError) as caught:
        benchmark_matrix.read_experiment("nope")
    assert caught.value.code == "MATRIX_NOT_FOUND"
    assert caught.value.status_code == 404


def test_list_experiments_reports_progress(experiments_root: Path) -> None:
    directory = experiments_root / "matrix-list"
    (directory / "artifacts").mkdir(parents=True)
    benchmark_matrix.write_json(
        directory / "state.json",
        {"experiment_id": "matrix-list", "pid": 999_999, "output_dir": str(directory)},
    )
    benchmark_matrix.write_json(directory / "plan.json", {"tasks": [1, 2, 3]})
    (directory / "artifacts" / "case__off__r1.json").write_text("{}", encoding="utf-8")

    entries = benchmark_matrix.list_experiments()

    assert [entry["experiment_id"] for entry in entries] == ["matrix-list"]
    assert entries[0]["planned_tasks"] == 3
    assert entries[0]["completed_tasks"] == 1


# --------------------------------------------------------------------------- API


@pytest.mark.asyncio
async def test_launch_endpoint_returns_the_initial_state(
    experiments_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeProcess:
        pid = 4242

    monkeypatch.setattr(benchmark_matrix.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(benchmark_matrix, "_process_is_running", lambda pid: True)

    response = await launch_matrix(
        MatrixLaunchRequest(
            experiment_id="matrix-api",
            case_dir=str(_case_dir(experiments_root.parent)),
            randomize_range="never",
            dry_run=True,
        )
    )

    assert response["data"]["experiment_id"] == "matrix-api"
    assert response["data"]["status"] == benchmark_matrix.STATUS_RUNNING


@pytest.mark.asyncio
async def test_status_endpoint_reads_the_state_back(experiments_root: Path) -> None:
    directory = experiments_root / "matrix-status"
    directory.mkdir()
    benchmark_matrix.write_json(
        directory / "state.json",
        {
            "experiment_id": "matrix-status",
            "pid": 999_999,
            "output_dir": str(directory),
            "started_at": datetime.now(UTC).isoformat(),
        },
    )

    response = await matrix_status("matrix-status")

    assert response["data"]["status"] == benchmark_matrix.STATUS_INTERRUPTED


def test_the_matrix_routes_are_registered() -> None:
    from app.main import app

    # ``app.routes`` keeps included routers as lazy wrappers in this FastAPI
    # version, so the OpenAPI document is what actually proves registration.
    paths = set(app.openapi()["paths"])

    assert "/api/v1/knowledge/experiment-matrix" in paths
    assert "/api/v1/knowledge/experiment-matrix/{experiment_id}" in paths


# --------------------------------------------------- wall-clock budget knobs


def _capturing_install(monkeypatch: pytest.MonkeyPatch, executor) -> dict:
    """Install the stub executor and record the kwargs the CLI passed it."""

    captured: dict = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return executor

    monkeypatch.setattr(matrix_cli, "RealRunExecutor", _factory)
    return captured


def test_the_runtime_caps_reach_the_executor(tmp_path: Path, monkeypatch) -> None:
    """An unsolved case burns the whole per-Run budget.

    Without a flag for it, the wall-clock cost of a matrix is only expressible
    by editing the executor, so the operator had no way to bound a long run.
    """

    executor = _StubExecutor()
    captured = _capturing_install(monkeypatch, executor)

    code = matrix_cli.main(
        [
            "--case-dir",
            str(_case_dir(tmp_path)),
            "--output-dir",
            str(tmp_path / "out"),
            "--runtime-seconds",
            "240",
            "--total-runtime-seconds",
            "600",
            "--dry-run",
            "--json",
        ]
    )

    assert code == matrix_cli.EXIT_OK
    assert captured["max_runtime_seconds"] == 240
    assert captured["max_total_runtime_seconds"] == 600


def test_an_unset_runtime_cap_is_forwarded_as_none(tmp_path: Path, monkeypatch) -> None:
    """``None`` must mean "use the default", never "zero"."""

    executor = _StubExecutor()
    captured = _capturing_install(monkeypatch, executor)

    code = matrix_cli.main(
        [
            "--case-dir",
            str(_case_dir(tmp_path)),
            "--output-dir",
            str(tmp_path / "out"),
            "--dry-run",
            "--json",
        ]
    )

    assert code == matrix_cli.EXIT_OK
    assert captured["max_runtime_seconds"] is None
    assert captured["max_total_runtime_seconds"] is None


@pytest.mark.parametrize("supplied", [None, 240])
def test_the_executor_turns_none_into_its_own_default(supplied) -> None:
    from app.benchmark.experiment import (
        DEFAULT_MAX_RUNTIME_SECONDS,
        DEFAULT_MAX_TOTAL_RUNTIME_SECONDS,
        RealRunExecutor,
    )

    executor = RealRunExecutor(
        max_runtime_seconds=supplied,
        max_total_runtime_seconds=supplied,
    )

    assert executor.max_runtime_seconds == (
        DEFAULT_MAX_RUNTIME_SECONDS if supplied is None else supplied
    )
    assert executor.max_total_runtime_seconds == (
        DEFAULT_MAX_TOTAL_RUNTIME_SECONDS if supplied is None else supplied
    )
    # The manifest records the effective value, not the ``None`` it was given.
    limits = executor.manifest_metadata()["limits"]
    assert limits["max_runtime_seconds"] == executor.max_runtime_seconds
    assert limits["max_total_runtime_seconds"] == executor.max_total_runtime_seconds
