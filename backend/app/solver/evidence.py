"""Solver-owned read adapter over the existing Evidence authority."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.multi_agent import EvidenceLedger


class SolverEvidenceAuthority:
    """Read-only Evidence reference verifier for Solver Completion.

    Evidence rows remain owned by the existing EvidenceLedger model.  This
    adapter only snapshots valid references for one evaluation; it never
    creates, edits, or replaces Evidence records.
    """

    def __init__(self, valid_refs: set[str]) -> None:
        self._valid_refs = frozenset(str(item) for item in valid_refs)

    @classmethod
    async def from_session(cls, session: AsyncSession, run_id: str) -> "SolverEvidenceAuthority":
        rows = await session.scalars(
            select(EvidenceLedger.id).where(
                EvidenceLedger.run_id == run_id,
                EvidenceLedger.status.in_(["VERIFIED", "ACTIVE"]),
            )
        )
        return cls({str(item) for item in rows})

    def verify_refs(self, evidence_refs: Sequence[str]) -> bool:
        refs = {str(item) for item in evidence_refs if str(item)}
        return bool(refs) and refs.issubset(self._valid_refs)


__all__ = ["SolverEvidenceAuthority"]
