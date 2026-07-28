import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, FlagProvenance, SolveRun
from app.orchestration.state_machine import RunStatus
from app.services.events import event_service
from app.services.verified_flag_stop import verified_flag_stop_controller


class FlagService:
    _STRICT_DEFAULT_FLAG = re.compile(r"(?i)flag\{[^{}\r\n\"\\]{1,256}\}")

    @staticmethod
    def _is_displayable(candidate: str, pattern: str) -> bool:
        if (
            not candidate
            or len(candidate) > 512
            or any(char in candidate for char in ('\r', '\n', '"', "'", "\\"))
            or candidate.count("{") != candidate.count("}")
        ):
            return False
        try:
            return re.fullmatch(pattern, candidate) is not None
        except re.error:
            return False

    @classmethod
    def _extract_matches(cls, pattern: str, content: str) -> list[str]:
        """Extract stable, displayable candidates without crossing JSON/text boundaries."""
        try:
            compiled = re.compile(pattern)
        except re.error:
            return []
        matches: list[str] = []
        for match in compiled.finditer(content):
            candidate = match.group(0)
            # A permissive ``[^}]+`` pattern can consume an incomplete token,
            # escaped JSON, and the next real flag. Never persist that blob.
            if not cls._is_displayable(candidate, pattern) or candidate.count("{") != 1:
                continue
            if candidate not in matches:
                matches.append(candidate)
        # Recover a valid default-shaped flag nested inside a serialized tool
        # result that was already captured by an older permissive regex.
        if not matches and "flag\\{" in pattern.replace(" ", ""):
            for match in cls._STRICT_DEFAULT_FLAG.finditer(content):
                candidate = match.group(0)
                if candidate not in matches:
                    matches.append(candidate)
        return matches

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
        if run.current_phase not in {"INTAKE", "BASELINE", "MAPPING", "HYPOTHESIS", "TESTING", "CHAINING", "FLAG_SEARCH", "FLAG_VERIFICATION", "REPORTING"}:
            run.current_phase = "REPORTING"
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
        *,
        source_tool_call_id: str | None = None,
        source_agent_task_id: str | None = None,
        source_type: str = "TOOL_ARTIFACT",
    ) -> list[FlagCandidate]:
        matches = self._extract_matches(challenge.flag_pattern, content)
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
                first_seen_source_type=source_type,
                first_seen_source_id=artifact.id,
                first_seen_at=datetime.now(UTC),
                source_tool_call_id=source_tool_call_id or artifact.tool_call_id,
                source_agent_task_id=source_agent_task_id,
                source_assistance_level=run.assistance_level or "AUTONOMOUS",
            )
            session.add(item)
            await session.flush()
            session.add(
                FlagProvenance(
                    run_id=run.id,
                    candidate_id=item.id,
                    first_seen_source_type=source_type,
                    first_seen_source_id=artifact.id,
                    first_seen_at=item.first_seen_at or datetime.now(UTC),
                    source_artifact_id=artifact.id,
                    source_tool_call_id=source_tool_call_id or artifact.tool_call_id,
                    source_agent_task_id=source_agent_task_id,
                    source_assistance_level=run.assistance_level or "AUTONOMOUS",
                    source_is_autonomous=(run.assistance_level or "AUTONOMOUS") in {"AUTONOMOUS", "HINT_GUIDED"},
                )
            )
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
                first_seen_source_type="USER_INPUT",
                first_seen_source_id=None,
                first_seen_at=datetime.now(UTC),
                source_assistance_level=run.assistance_level or "ANSWER_GUIDED",
            )
            session.add(item)
            await session.flush()
            session.add(
                FlagProvenance(
                    run_id=run.id,
                    candidate_id=item.id,
                    first_seen_source_type="USER_INPUT",
                    first_seen_at=item.first_seen_at or datetime.now(UTC),
                    source_assistance_level=run.assistance_level or "ANSWER_GUIDED",
                    source_is_autonomous=False,
                )
            )
        else:
            item.verified = valid
            item.review_state = "VALID" if valid else "INVALID"
            provenance = await session.scalar(select(FlagProvenance).where(FlagProvenance.candidate_id == item.id))
            if provenance:
                provenance.verification_source_type = "FRESH_REPRODUCTION"
                provenance.verification_source_id = item.source_artifact_id
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
            verified_event = await event_service.append(session, run.id, "flag.verified", {"candidate_id": item.id})
            # Verification is the authoritative terminal signal.  The stop
            # controller closes the attempt and removes only its Lease; it
            # deliberately does not claim that the SDK execution was
            # cancelled.
            await verified_flag_stop_controller.stop(session, run, candidate_id=item.id, terminal_event_sequence=verified_event.sequence)
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
        item.review_state = normalized
        item.verified = normalized == "VALID"
        if normalized == "VALID" and self._should_override_run_status(run):
            await self._mark_run_solved(session, run)
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
            verified_event = await event_service.append(session, run.id, "flag.verified", {"candidate_id": item.id})
            await verified_flag_stop_controller.stop(session, run, candidate_id=item.id, terminal_event_sequence=verified_event.sequence)
        return item


flag_service = FlagService()
