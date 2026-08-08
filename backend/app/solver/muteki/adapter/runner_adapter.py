from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RunnerResult:
    success: bool
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None
    error_code: str | None = None


class RunnerAdapter:
    """Small, normalized facade over the existing RunnerClient."""

    def __init__(self, runner_client: Any | None = None, *, max_output_chars: int = 12000) -> None:
        if runner_client is None:
            from app.services.runner_client import runner_client as default_runner_client

            runner_client = default_runner_client
        self._runner = runner_client
        self.max_output_chars = max(1000, int(max_output_chars))

    async def run_script(
        self,
        script_path: str,
        args: list[str],
        workspace_id: str,
        run_id: str,
        timeout_seconds: int = 60,
    ) -> RunnerResult:
        return await self._run(run_id, workspace_id, "script_run", {"path": script_path, "args": list(args), "timeout_seconds": timeout_seconds})

    async def run_python(
        self,
        code: str,
        workspace_id: str,
        run_id: str,
        timeout_seconds: int = 60,
    ) -> RunnerResult:
        return await self._run(run_id, workspace_id, "python_run", {"code": str(code), "timeout_seconds": timeout_seconds, "network_mode": "none"})

    async def _run(self, run_id: str, workspace_id: str, tool: str, arguments: dict[str, Any]) -> RunnerResult:
        try:
            job_id = await self._runner.create_job(str(run_id), [], tool, {**arguments, "workspace_id": str(workspace_id)})
            job_id = str(job_id or "")
            if not job_id:
                return RunnerResult(False, "FAILED", error_code="RUNNER_JOB_ID_MISSING")
            try:
                result = await self._runner.wait_job(job_id, tool_timeout_seconds=min(600, max(1, int(arguments.get("timeout_seconds", 60)))))
            except TypeError:
                result = await self._runner.wait_job(job_id)
            payload = dict(result) if isinstance(result, dict) else {"result": result}
            payload = self._truncate(payload)
            status = str(payload.get("status") or payload.get("result_status") or "FAILED").upper()
            return RunnerResult(status in {"COMPLETED", "SUCCESS", "CACHED"}, status, payload, job_id, payload.get("error_code"))
        except Exception as error:
            return RunnerResult(False, "FAILED", {"summary": str(error)[:1000]}, error_code="RUNNER_ADAPTER_ERROR")

    def _truncate(self, value: Any) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) <= self.max_output_chars:
            return value
        return {"summary": encoded[: self.max_output_chars], "truncated": True}


__all__ = ["RunnerAdapter", "RunnerResult"]
