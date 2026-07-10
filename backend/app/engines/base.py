from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class EngineEvent:
    event_type: str
    payload: dict = field(default_factory=dict)
    status: str | None = None


class SolveEngine(ABC):
    @abstractmethod
    async def start(self, run_id: str) -> AsyncIterator[EngineEvent]: ...

    @abstractmethod
    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]: ...

    @abstractmethod
    async def cancel(self, run_id: str) -> None: ...

    @abstractmethod
    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]: ...
