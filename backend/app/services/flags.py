import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, SolveRun
from app.orchestration.state_machine import RunStatus
from app.services.events import event_service


class FlagService:
    async def _mark_challenge_solved(self, session: AsyncSession, run: SolveRun) -> None:
        challenge = await session.get(Challenge, run.challenge_id)
        if challenge and challenge.status != "SOLVED":
            challenge.status = "SOLVED"

    @staticmethod
    def _should_override_run_status(run: SolveRun) -> bool:
        return run.status.startswith("FAILED") or run.status == RunStatus.COMPLETED_UNSOLVED.value

    async def _mark_run_solved(self, session: AsyncSession, run: SolveRun) -> bool:
        if run.status == RunStatus.COMPLETED_SOLVED:
            return False
        run.status = RunStatus.COMPLETED_SOLVED.value
        run.current_phase = RunStatus.COMPLETED_SOLVED.value
        await self._mark_challenge_solved(session, run)
        return True

    async def reconcile_run_status(self, session: AsyncSession, run: SolveRun) -> bool:
        has_valid_flag = await session.scalar(
            select(FlagCandidate.id).where(
                FlagCandidate.run_id == run.id, FlagCandidate.review_state == "VALID"
            )
        )
        if not has_valid_flag:
            return False
        changed = False
        if run.status != RunStatus.COMPLETED_SOLVED.value:
            changed = await self._mark_run_solved(session, run)
        else:
            challenge = await session.get(Challenge, run.challenge_id)
            if challenge and challenge.status != "SOLVED":
                await self._mark_challenge_solved(session, run)
                changed = True
        if changed:
            await session.commit()
            await session.refresh(run)
        return changed

    async def extract_candidates(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        artifact: Artifact,
        content: str,
    ) -> list[FlagCandidate]:
        try:
            matches = set(re.findall(challenge.flag_pattern, content))
        except re.error:
            matches = set()
        candidates: list[FlagCandidate] = []
        for candidate in matches:
            existing = await session.scalar(
                select(FlagCandidate).where(
                    FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
                )
            )
            if existing:
                continue
            item = FlagCandidate(
                run_id=run.id,
                candidate=candidate,
                source_artifact_id=artifact.id,
                pattern_matched=True,
                review_state="OPEN",
            )
            session.add(item)
            candidates.append(item)
        await session.commit()
        for item in candidates:
            await event_service.append(
                session,
                run.id,
                "flag.candidate_found",
                {"candidate_id": item.id, "artifact_id": artifact.id},
            )
        return candidates

    async def verify(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, candidate: str
    ) -> bool:
        try:
            valid = re.fullmatch(challenge.flag_pattern, candidate) is not None
        except re.error:
            valid = False
        item = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
            )
        )
        if item is None:
            item = FlagCandidate(
                run_id=run.id,
                candidate=candidate,
                pattern_matched=valid,
                verified=valid,
                review_state="VALID" if valid else "INVALID",
            )
            session.add(item)
        else:
            item.verified = valid
            item.review_state = "VALID" if valid else "INVALID"
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "flag.reviewed",
            {
                "candidate_id": item.id,
                "candidate": item.candidate,
                "review_state": item.review_state,
                "verified": item.verified,
                "source": "auto",
            },
        )
        if valid:
            changed = False
            if self._should_override_run_status(run):
                changed = await self._mark_run_solved(session, run)
            await session.commit()
            if changed:
                await event_service.append(
                    session, run.id, "run.status_changed", {"status": run.status}
                )
            await event_service.append(session, run.id, "flag.verified", {"candidate_id": item.id})
        return valid

    async def set_review_state(
        self,
        session: AsyncSession,
        run: SolveRun,
        candidate_id: str,
        review_state: str,
    ) -> FlagCandidate:
        item = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id, FlagCandidate.id == candidate_id
            )
        )
        if item is None:
            raise ValueError("flag candidate not found")
        normalized = review_state.upper()
        if normalized not in {"OPEN", "VALID", "INVALID"}:
            raise ValueError("invalid review state")
        changed = False
        item.review_state = normalized
        item.verified = normalized == "VALID"
        if normalized == "VALID" and self._should_override_run_status(run):
            changed = await self._mark_run_solved(session, run)
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "flag.reviewed",
            {
                "candidate_id": item.id,
                "candidate": item.candidate,
                "review_state": item.review_state,
                "verified": item.verified,
                "source": "manual",
            },
        )
        if item.verified:
            if normalized == "VALID" and changed:
                await event_service.append(
                    session, run.id, "run.status_changed", {"status": run.status}
                )
            await event_service.append(session, run.id, "flag.verified", {"candidate_id": item.id})
        return item


flag_service = FlagService()
