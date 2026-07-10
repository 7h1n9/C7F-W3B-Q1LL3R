from app.executors.base import ExecutionBackend
from app.executors.file_executor import file_read, file_search
from app.executors.http_executor import execute_http
from app.executors.python_executor import python_run
from app.models import JobRequest


class KaliVmExecutionBackend(ExecutionBackend):
    async def execute(self, request: JobRequest) -> dict:
        handlers = {"http_request": execute_http, "file_read": file_read, "file_search": file_search, "python_run": python_run}
        return await handlers[request.tool](request)

    async def cancel(self, job_id: str) -> None:
        return None


class DockerExecutionBackend(ExecutionBackend):
    async def execute(self, request: JobRequest) -> dict:
        raise NotImplementedError("Docker sandbox is reserved for a future release")

    async def cancel(self, job_id: str) -> None:
        return None
