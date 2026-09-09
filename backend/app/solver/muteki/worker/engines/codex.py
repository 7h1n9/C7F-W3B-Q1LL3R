from __future__ import annotations

from .cli import CliWorkerEngine


class CodexCliEngine(CliWorkerEngine):
    """Headless Codex CLI adapter used by the standalone process driver."""

    binary = "codex"

    def engine_type(self) -> str:
        return "codex"

    def _command(self, prompt: str) -> list[str]:
        return [
            self.executable,
            "exec",
            "--json",
            "--ignore-rules",
            "--dangerously-bypass-approvals-and-sandbox",
            "--",
            prompt,
        ]


__all__ = ["CodexCliEngine"]
