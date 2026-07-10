from abc import ABC, abstractmethod

from app.models import JobRequest


class ExecutionBackend(ABC):
    @abstractmethod
    async def execute(self, request: JobRequest) -> dict: ...

    @abstractmethod
    async def cancel(self, job_id: str) -> None: ...
