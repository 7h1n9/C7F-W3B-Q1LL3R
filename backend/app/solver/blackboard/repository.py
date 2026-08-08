from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import BlackboardState


class BlackboardVersionConflict(RuntimeError):
    """Raised when an update is based on a stale Blackboard version."""


class BlackboardRepository(Protocol):
    """Async durable repository contract for Solver Core."""

    async def save(self, state: BlackboardState) -> BlackboardState: ...

    async def load(self, run_id: str) -> BlackboardState | None: ...

    async def update(
        self,
        run_id: str,
        patch: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BlackboardState: ...


def apply_patch(state: BlackboardState, patch: Mapping[str, Any]) -> BlackboardState:
    """Apply a narrow, typed patch without exposing ORM rows to callers."""
    data = state.model_dump(mode="python")
    for field in (
        "phase",
        "goal",
        "knowledge",
        "control",
        "history",
        "evidence_refs",
        "vulnerability_hypotheses",
    ):
        if field in patch:
            data[field] = patch[field]
    if "knowledge_merge" in patch:
        data["knowledge"] = {**data["knowledge"], **dict(patch["knowledge_merge"])}
    if "control_merge" in patch:
        data["control"] = {**data["control"], **dict(patch["control_merge"])}
    if "history_append" in patch:
        data["history"] = [*data["history"], *list(patch["history_append"])]
    if "evidence_refs_append" in patch:
        data["evidence_refs"] = [*data["evidence_refs"], *map(str, patch["evidence_refs_append"])]
    data["version"] = state.version + 1
    return BlackboardState.model_validate(data)
