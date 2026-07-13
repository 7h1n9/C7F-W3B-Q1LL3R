import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.run import AgentTurn, RunAttempt, RunExecutionLease, SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RunAttemptService:
    lease_ttl_seconds = 90
    owner_instance_id = f"{socket.gethostname()}:{os.getpid()}"

    async def _purge_expired_lease(self, session: AsyncSession, run_id: str) -> None:
        now = utc_now()
        leases = list(
            (
                await session.scalars(
                    select(RunExecutionLease).where(RunExecutionLease.run_id == run_id)
                )
            ).all()
        )
        for lease in leases:
            expires_at = ensure_aware(lease.expires_at)
            if expires_at and expires_at <= now:
                attempt = await session.get(RunAttempt, lease.attempt_id)
                if attempt and attempt.status == "RUNNING":
                    attempt.status = "ABORTED"
                    attempt.error_code = "PROCESS_INTERRUPTED"
                    attempt.finished_at = now
                    attempt.heartbeat_at = ensure_aware(attempt.heartbeat_at) or now
                await session.delete(lease)
        if leases:
            await session.commit()

    async def begin(self, session: AsyncSession, run: SolveRun) -> tuple[RunAttempt, RunExecutionLease]:
        await self._purge_expired_lease(session, run.id)
        existing = await session.scalar(
            select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)
        )
        if existing:
            raise DomainError(
                "RUN_ALREADY_EXECUTING",
                "Run already has an active execution lease.",
                status_code=409,
            )
        previous_attempts = await session.scalar(
            select(RunAttempt).where(RunAttempt.run_id == run.id).order_by(RunAttempt.attempt_number.desc())
        )
        now = utc_now()
        input_tokens = await session.scalar(
            select(func.coalesce(func.sum(AgentTurn.input_tokens), 0)).where(AgentTurn.run_id == run.id)
        )
        output_tokens = await session.scalar(
            select(func.coalesce(func.sum(AgentTurn.output_tokens), 0)).where(AgentTurn.run_id == run.id)
        )
        attempt = RunAttempt(
            run_id=run.id,
            attempt_number=(previous_attempts.attempt_number if previous_attempts else 0) + 1,
            engine_type=run.engine_type,
            model_config_id=run.model_config_id,
            started_at=now,
            heartbeat_at=now,
            status="RUNNING",
            initial_agent_steps=run.agent_step_count,
            initial_tool_calls=run.tool_call_count,
            initial_input_tokens=int(input_tokens or 0),
            initial_output_tokens=int(output_tokens or 0),
        )
        session.add(attempt)
        await session.flush()
        lease = RunExecutionLease(
            run_id=run.id,
            attempt_id=attempt.id,
            owner_instance_id=self.owner_instance_id,
            lease_token=str(uuid.uuid4()),
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=self.lease_ttl_seconds),
        )
        session.add(lease)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise DomainError(
                "RUN_ALREADY_EXECUTING",
                "Run already has an active execution lease.",
                status_code=409,
            ) from error
        await session.refresh(attempt)
        await session.refresh(lease)
        return attempt, lease

    async def heartbeat(
        self, session: AsyncSession, attempt: RunAttempt | None, lease: RunExecutionLease | None
    ) -> None:
        if not attempt or not lease:
            return
        now = utc_now()
        attempt.heartbeat_at = now
        lease.heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        await session.commit()

    async def finish(
        self,
        session: AsyncSession,
        run: SolveRun,
        attempt: RunAttempt | None,
        lease: RunExecutionLease | None,
    ) -> None:
        if attempt is not None:
            turns = list((await session.scalars(select(AgentTurn).where(AgentTurn.run_id == run.id))).all())
            attempt.finished_at = utc_now()
            attempt.status = str(run.status)
            attempt.error_code = run.last_error_code
            attempt.agent_steps = max(0, run.agent_step_count - (attempt.initial_agent_steps or 0))
            attempt.tool_calls = max(0, run.tool_call_count - (attempt.initial_tool_calls or 0))
            attempt.input_tokens = max(0, sum(item.input_tokens or 0 for item in turns) - (attempt.initial_input_tokens or 0))
            attempt.output_tokens = max(0, sum(item.output_tokens or 0 for item in turns) - (attempt.initial_output_tokens or 0))
            attempt.heartbeat_at = utc_now()
        if lease is not None:
            current = await session.get(RunExecutionLease, lease.id)
            if current is not None:
                await session.delete(current)
        await session.commit()

    async def reconcile_startup(self, session: AsyncSession) -> dict:
        now = utc_now()
        aborted_attempts = 0
        closed_attempts = 0
        leases_deleted = await session.execute(delete(RunExecutionLease))
        attempts = list((await session.scalars(select(RunAttempt).where(RunAttempt.status == "RUNNING"))).all())
        for attempt in attempts:
            run = await session.get(SolveRun, attempt.run_id)
            if run and RunStatus(run.status) in TERMINAL:
                attempt.status = run.status
                attempt.error_code = run.last_error_code
                attempt.finished_at = ensure_aware(run.finished_at) or now
                closed_attempts += 1
            else:
                attempt.status = "ABORTED"
                attempt.error_code = "PROCESS_INTERRUPTED"
                attempt.finished_at = now
                aborted_attempts += 1
        await session.commit()
        return {
            "leases_deleted": leases_deleted.rowcount or 0,
            "attempts_aborted": aborted_attempts,
            "attempts_closed": closed_attempts,
        }


run_attempt_service = RunAttemptService()
