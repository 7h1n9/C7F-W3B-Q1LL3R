"""Production construction boundary for Solver run context."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .context import RunContext


class RunContextFactory:
    """Build a Solver-safe context without performing ORM or database work."""

    def __init__(
        self,
        *,
        security_policy_id: str = "solver-default",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.security_policy_id = security_policy_id
        self.metadata = dict(metadata or {})

    def build(self, challenge: Any, run: Any) -> RunContext:
        """Create a context from already-loaded Challenge and SolveRun models."""
        context = RunContext.from_models(run, challenge)
        merged_metadata = {**dict(context.metadata), **self.metadata}
        return replace(
            context,
            security_policy_id=self.security_policy_id,
            metadata=merged_metadata,
        )

    def from_models(self, run: Any, challenge: Any) -> RunContext:
        """Compatibility spelling matching ``RunContext.from_models``."""
        return self.build(challenge, run)
