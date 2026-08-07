from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import SolveRun

from .models import BlackboardState
from .repository import BlackboardRepository, BlackboardVersionConflict, apply_patch
from .serializer import deserialize_state, serialize_state

CHECKPOINT_KEY = "solver_blackboard"


class SolveRunBlackboardStore(BlackboardRepository):
    """Persist Solver state inside the existing SolveRun checkpoint JSON.

    The Solver Blackboard is control state, not an Evidence Store.  Keeping it
    under an existing JSON column avoids a new table/schema migration while
    preserving unrelated legacy checkpoint keys.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, state: BlackboardState) -> BlackboardState:
        run = await self.session.get(SolveRun, state.run_id)
        if run is None:
            raise KeyError(f"Run not found: {state.run_id}")
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint[CHECKPOINT_KEY] = serialize_state(state)
        run.recovery_checkpoint_json = checkpoint
        await self.session.flush()
        return state.copy_for_read()

    async def load(self, run_id: str) -> BlackboardState | None:
        run = await self.session.get(SolveRun, run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        checkpoint = run.recovery_checkpoint_json or {}
        payload = checkpoint.get(CHECKPOINT_KEY) if isinstance(checkpoint, Mapping) else None
        if not isinstance(payload, Mapping):
            return None
        return deserialize_state(payload)

    async def update(
        self,
        run_id: str,
        patch: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BlackboardState:
        current = await self.load(run_id)
        if current is None:
            raise KeyError(f"Blackboard not found for run {run_id!r}")
        if expected_version is not None and current.version != expected_version:
            raise BlackboardVersionConflict(
                f"Expected Blackboard version {expected_version}, got {current.version}"
            )
        return await self.save(apply_patch(current, patch))
