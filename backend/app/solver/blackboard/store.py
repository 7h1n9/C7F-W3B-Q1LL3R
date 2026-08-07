from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol

from .models import BlackboardState


class BlackboardStore(Protocol):
    """Persistence contract for the Blackboard.

    Phase 1.1 uses the in-memory implementation.  A later migration may add a
    durable adapter without changing Coordinator, Planner, or Worker APIs.
    """

    def load(self, run_id: str) -> BlackboardState | None: ...

    def save(self, state: BlackboardState) -> BlackboardState: ...


class InMemoryBlackboardStore:
    """Small deterministic store used by the skeleton and unit tests."""

    def __init__(self) -> None:
        self._states: dict[str, BlackboardState] = {}

    def load(self, run_id: str) -> BlackboardState | None:
        state = self._states.get(run_id)
        return state.copy_for_read() if state else None

    def save(self, state: BlackboardState) -> BlackboardState:
        stored = state.copy_for_read()
        self._states[state.run_id] = stored
        return stored.copy_for_read()


class Blackboard:
    """Facade for versioned Blackboard reads and writes."""

    def __init__(self, store: BlackboardStore | None = None) -> None:
        self.store = store or InMemoryBlackboardStore()

    def initialize(
        self,
        run_id: str,
        *,
        phase: str = "BASELINE",
        allowed_actions: list[str] | None = None,
    ) -> BlackboardState:
        existing = self.store.load(run_id)
        if existing is not None:
            return existing
        state = BlackboardState(
            run_id=run_id,
            phase=phase,
            allowed_actions=list(allowed_actions or []),
        )
        return self.store.save(state)

    def read(self, run_id: str) -> BlackboardState:
        state = self.store.load(run_id)
        if state is None:
            raise KeyError(f"Blackboard not initialized for run {run_id!r}")
        return state

    def update(
        self,
        run_id: str,
        *,
        phase: str | None = None,
        allowed_actions: list[str] | None = None,
        facts: list[Mapping[str, Any]] | None = None,
        hypotheses: list[Mapping[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        event: Mapping[str, Any] | None = None,
    ) -> BlackboardState:
        current = self.read(run_id)
        next_state = current.model_copy(deep=True)
        next_state.version += 1
        if phase is not None:
            next_state.phase = phase
        if allowed_actions is not None:
            next_state.allowed_actions = list(allowed_actions)
        if facts:
            next_state.facts.extend(deepcopy(list(facts)))
        if hypotheses:
            next_state.hypotheses.extend(deepcopy(list(hypotheses)))
        if evidence_refs:
            next_state.evidence_refs.extend(str(item) for item in evidence_refs)
        if event is not None:
            next_state.history.append(deepcopy(dict(event)))
        return self.store.save(next_state)
