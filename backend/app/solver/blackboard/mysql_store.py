from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

from .models import BlackboardState
from .repository import (
    BlackboardRepository,
    BlackboardVersionConflict,
    apply_patch,
)
from .serializer import deserialize_state, serialize_state


class BlackboardRecord(Base):
    """Storage declaration for the future ``solver_blackboards`` table.

    The model is not imported into the application model registry and no
    migration is created in Phase 1.2.  This keeps the adapter testable without
    connecting it to a real Run or changing the existing database schema.
    """

    __tablename__ = "solver_blackboards"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class MySQLBlackboardStore(BlackboardRepository):
    """Persist Blackboard JSON through an injected existing AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, state: BlackboardState) -> BlackboardState:
        record = await self.session.get(BlackboardRecord, state.run_id)
        if record is not None and state.version < int(record.version):
            raise BlackboardVersionConflict(
                f"stale Blackboard {state.run_id!r}: {state.version} < {record.version}"
            )
        payload = serialize_state(state)
        if record is None:
            record = BlackboardRecord(
                run_id=state.run_id,
                schema_version=state.schema_version,
                version=state.version,
                state_json=payload,
            )
            self.session.add(record)
        else:
            record.schema_version = state.schema_version
            record.version = state.version
            record.state_json = payload
            record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return deserialize_state(record.state_json)

    async def load(self, run_id: str) -> BlackboardState | None:
        record = await self.session.get(BlackboardRecord, run_id)
        if record is None:
            return None
        return deserialize_state(record.state_json)

    async def update(
        self,
        run_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BlackboardState:
        current = await self.load(run_id)
        if current is None:
            raise KeyError(f"Blackboard not found for run {run_id!r}")
        if expected_version is not None and current.version != expected_version:
            raise BlackboardVersionConflict(
                f"stale Blackboard {run_id!r}: expected {expected_version}, found {current.version}"
            )
        return await self.save(apply_patch(current, patch))
