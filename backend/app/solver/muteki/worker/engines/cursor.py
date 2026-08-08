from __future__ import annotations

from .cli import CliWorkerEngine


class CursorEngine(CliWorkerEngine):
    binary = "cursor-agent"

    def engine_type(self) -> str:
        return "cursor"

    def _command(self, prompt: str) -> list[str]:
        return [self.executable, "-p", "--force", "--trust", prompt]


__all__ = ["CursorEngine"]
