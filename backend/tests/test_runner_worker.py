from __future__ import annotations

from typing import Any

from app.solver.action import ActionIntent
from app.solver.observation import SolverObservation
from app.solver.reducers.web import WebObservationReducer
from app.solver.worker import RunnerWorker, WorkerManager


class FakeRunnerClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.create_calls: list[tuple[str, list[str], str, dict[str, Any]]] = []
        self.wait_calls: list[tuple[str, int | None]] = []

    async def create_job(
        self,
        run_id: str,
        allowed_hosts: list[str],
        tool: str,
        arguments: dict[str, Any],
    ) -> str:
        self.create_calls.append((run_id, allowed_hosts, tool, arguments))
        return "job-1"

    async def wait_job(self, job_id: str, *, tool_timeout_seconds: int | None = None) -> dict[str, Any]:
        self.wait_calls.append((job_id, tool_timeout_seconds))
        return self.result


def runner_action(action_name: str = "http_request") -> ActionIntent:
    return ActionIntent(
        action_name=action_name,
        reason="runner adapter test",
        parameters={"method": "GET", "url": "http://target/search"},
        metadata={
            "backend": "runner",
            "run_id": "run-1",
            "allowed_hosts": ["target"],
            "timeout_seconds": 20,
        },
    )


async def test_runner_worker_converts_http_action_to_runner_request() -> None:
    client = FakeRunnerClient({"status": "COMPLETED", "status_code": 200, "body": "ok"})
    worker = RunnerWorker(client)

    result = await worker.execute(runner_action())

    assert client.create_calls == [
        ("run-1", ["target"], "http_request", {"method": "GET", "url": "http://target/search"})
    ]
    assert client.wait_calls == [("job-1", 20)]
    assert result.success is True
    assert result.action_name == "http_request"
    assert result.output["status_code"] == 200
    assert result.metadata["job_id"] == "job-1"


async def test_runner_worker_converts_boolean_success_response() -> None:
    client = FakeRunnerClient(
        {
            "status": "SUCCESS",
            "structured_result": {"stable_true": True, "stable_false": False},
        }
    )
    worker = RunnerWorker(client)

    action = ActionIntent(
        action_name="sql_boolean_compare",
        reason="runner boolean test",
        parameters={"test_field": "department"},
        metadata=runner_action("sql_boolean_compare").metadata,
    )
    result = await worker.execute(action)

    assert result.success is True
    assert result.action_name == "sql_boolean_compare"
    assert result.output["structured_result"]["stable_true"] is True
    observation = SolverObservation.from_worker_result(action, result)
    reduction = WebObservationReducer().reduce(observation)
    assert {item["type"] for item in reduction.verified_facts} == {
        "BOOLEAN_ORACLE",
        "VALIDATION_SUCCESS",
    }


async def test_runner_worker_converts_runner_failure_without_raising() -> None:
    client = FakeRunnerClient(
        {"status": "FAILED", "error_code": "TARGET_UNAVAILABLE", "error": "offline"}
    )
    worker = RunnerWorker(client)

    result = await worker.execute(runner_action())

    assert result.success is False
    assert result.metadata["status"] == "FAILED"
    assert result.metadata["error_code"] == "TARGET_UNAVAILABLE"
    assert result.output["error"] == "offline"


async def test_runner_worker_rejects_unsupported_action_without_runner_call() -> None:
    client = FakeRunnerClient({"status": "COMPLETED"})
    worker = RunnerWorker(client)

    result = await worker.execute(runner_action("content_discovery"))

    assert result.success is False
    assert result.metadata["status"] == "UNSUPPORTED_ACTION"
    assert result.metadata["error_code"] == "WORKER_ACTION_UNSUPPORTED"
    assert client.create_calls == []


async def test_worker_manager_routes_runner_backend_to_runner_worker() -> None:
    client = FakeRunnerClient({"status": "COMPLETED", "status_code": 200})
    runner_worker = RunnerWorker(client)
    manager = WorkerManager(workers={"runner": runner_worker})

    result = await manager.execute(runner_action())

    assert result.success is True
    assert client.create_calls[0][2] == "http_request"
