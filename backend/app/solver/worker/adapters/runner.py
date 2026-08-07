from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...action import ActionIntent
from ..interface import Worker, WorkerResult


class RunnerWorker(Worker):
    """Adapt Solver actions to the existing RunnerClient job interface."""

    SUPPORTED_ACTIONS = frozenset({"http_request", "sql_boolean_compare"})
    SUCCESS_STATUSES = frozenset({"COMPLETED", "SUCCESS", "CACHED"})

    def __init__(self, runner_client: Any | None = None) -> None:
        if runner_client is None:
            from app.services.runner_client import runner_client as default_runner_client

            runner_client = default_runner_client
        self.runner_client = runner_client

    async def execute(self, action: ActionIntent) -> WorkerResult:
        if action.action_name not in self.SUPPORTED_ACTIONS:
            return self._failure(
                action,
                status="UNSUPPORTED_ACTION",
                error_code="WORKER_ACTION_UNSUPPORTED",
            )

        run_id = str(action.metadata.get("run_id") or "").strip()
        if not run_id:
            return self._failure(
                action,
                status="INVALID_REQUEST",
                error_code="RUN_ID_REQUIRED",
            )

        arguments = dict(action.parameters)
        allowed_hosts = list(action.metadata.get("allowed_hosts") or [])
        timeout_seconds = action.metadata.get("timeout_seconds") or arguments.get("timeout_seconds")

        try:
            job_id = await self.runner_client.create_job(
                run_id,
                allowed_hosts,
                action.action_name,
                arguments,
            )
            if isinstance(job_id, Mapping):
                job_id = job_id.get("runner_job_id") or job_id.get("job_id") or job_id.get("task_id")
            job_id = str(job_id or "").strip()
            if not job_id:
                return self._failure(
                    action,
                    status="FAILED",
                    error_code="RUNNER_JOB_ID_MISSING",
                )

            result = await self._wait_for_job(job_id, timeout_seconds)
            return self._convert_result(action, result, job_id)
        except Exception as error:  # Runner failures are WorkerResult data, never loop exceptions.
            return self._failure(
                action,
                status="FAILED",
                error_code="RUNNER_WORKER_ERROR",
                error=str(error),
            )

    async def _wait_for_job(self, job_id: str, timeout_seconds: Any) -> dict[str, Any]:
        if timeout_seconds is None:
            result = await self.runner_client.wait_job(job_id)
        else:
            try:
                result = await self.runner_client.wait_job(
                    job_id,
                    tool_timeout_seconds=min(600, int(timeout_seconds)),
                )
            except TypeError as error:
                if "tool_timeout_seconds" not in str(error):
                    raise
                result = await self.runner_client.wait_job(job_id)
        return dict(result) if isinstance(result, Mapping) else {"result": result}

    def _convert_result(self, action: ActionIntent, result: dict[str, Any], job_id: str) -> WorkerResult:
        status = str(result.get("result_status") or result.get("status") or "FAILED").upper()
        metadata = {
            "backend": "runner",
            "status": status,
            "job_id": job_id,
        }
        for key in ("error_code", "stage", "retryable"):
            if key in result:
                metadata[key] = result[key]
        return WorkerResult(
            success=status in self.SUCCESS_STATUSES,
            action_name=action.action_name,
            output=result,
            metadata=metadata,
        )

    @staticmethod
    def _failure(
        action: ActionIntent,
        *,
        status: str,
        error_code: str,
        error: str | None = None,
    ) -> WorkerResult:
        metadata: dict[str, Any] = {
            "backend": "runner",
            "status": status,
            "error_code": error_code,
        }
        if error:
            metadata["error"] = error
        return WorkerResult(
            success=False,
            action_name=action.action_name,
            output={},
            metadata=metadata,
        )


RunnerAdapter = RunnerWorker
