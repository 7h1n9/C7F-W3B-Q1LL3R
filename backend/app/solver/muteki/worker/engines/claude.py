from __future__ import annotations

from .cli import CliWorkerEngine


class ClaudeEngine(CliWorkerEngine):
    binary = "claude"

    def engine_type(self) -> str:
        return "claude"

    def _command(self, prompt: str) -> list[str]:
        return [self.executable, "--print", "--dangerously-skip-permissions", prompt]


__all__ = ["ClaudeEngine"]
