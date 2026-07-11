import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, SolveRun
from app.services.events import event_service


class FlagService:
    async def extract_candidates(self, session: AsyncSession, run: SolveRun, challenge: Challenge, artifact: Artifact, content: str) -> list[FlagCandidate]:
        try:
            matches = set(re.findall(challenge.flag_pattern, content))
        except re.error:
            matches = set()
        candidates: list[FlagCandidate] = []
        for candidate in matches:
            existing = await session.scalar(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate))
            if existing:
                continue
            item = FlagCandidate(run_id=run.id, candidate=candidate, source_artifact_id=artifact.id, pattern_matched=True)
            session.add(item)
            candidates.append(item)
        await session.commit()
        for item in candidates:
            await event_service.append(session, run.id, "flag.candidate_found", {"candidate_id": item.id, "artifact_id": artifact.id})
        return candidates

    async def verify(self, session: AsyncSession, run: SolveRun, challenge: Challenge, candidate: str) -> bool:
        try:
            valid = re.fullmatch(challenge.flag_pattern, candidate) is not None
        except re.error:
            valid = False
        item = await session.scalar(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate))
        if item is None:
            item = FlagCandidate(run_id=run.id, candidate=candidate, pattern_matched=valid, verified=valid)
            session.add(item)
        else:
            item.verified = valid
        await session.commit()
        if valid:
            await event_service.append(session, run.id, "flag.verified", {"candidate_id": item.id})
        return valid


flag_service = FlagService()
