from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..graph import Fact


class EvidenceAdapter:
    """Preserve Muteki evidence references through the existing authority.

    Gateway execution already creates the authoritative EvidenceLedger row.
    For graph facts this adapter validates and returns those references; it
    never creates a parallel evidence table or changes the database schema.
    An injected writer can be used by deployments that expose a dedicated
    evidence-authority API.
    """

    def __init__(self, evidence_authority: Any | None = None, *, writer: Callable[..., Awaitable[str | None]] | None = None) -> None:
        self._authority = evidence_authority
        self._writer = writer

    async def verify_refs(self, refs: list[str] | tuple[str, ...], run_id: str) -> bool:
        if not refs:
            return False
        authority = self._authority
        if authority is None:
            return True
        verifier = getattr(authority, "verify_refs", None)
        if verifier is None:
            return True
        value = verifier(refs, run_id=run_id)
        if hasattr(value, "__await__"):
            value = await value
        return bool(value)

    async def write_fact(self, fact: Fact, run_id: str, source: str) -> str:
        refs = list(fact.evidence_refs)
        if self._writer is not None:
            value = await self._writer(fact=fact, run_id=run_id, source=source)
            return str(value or "")
        if await self.verify_refs(refs, run_id):
            return refs[0]
        return ""

    async def write_deadend(self, description: str, run_id: str, source: str) -> str:
        authority = self._authority
        recorder = getattr(authority, "record_dead_end", None) if authority is not None else None
        if recorder is None:
            return ""
        value = recorder(description=description, run_id=run_id, source=source)
        if hasattr(value, "__await__"):
            value = await value
        return str(value or "")


__all__ = ["EvidenceAdapter"]
