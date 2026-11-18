import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, FlagProvenance, SolveRun
from app.orchestration.state_machine import RunStatus
from app.services.events import event_service
from app.services.verified_flag_stop import verified_flag_stop_controller
from muteki.solver.gate import (
    GENERIC_FLAG_RE,
    is_placeholder_flag,
    matches_flag_format,
)


def _matches_declared(candidate: str, pattern: str) -> bool:
    """Did the candidate match the CHALLENGE's declared pattern?

    Distinct from "is this a flag": a candidate accepted only because it has the
    generic ``banner{body}`` shape is still worth recording, but the UI should be
    able to tell the operator their banner is wrong rather than silently treating
    the two as equivalent.
    """

    try:
        return re.fullmatch(pattern, candidate) is not None
    except re.error:
        return False


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
        # Either the challenge's own format or the generic flag shape.  The
        # declared pattern is a preference, not a filter: see GENERIC_FLAG_PATTERN.
        return _matches_declared(candidate, pattern) or (
            GENERIC_FLAG_RE.fullmatch(candidate) is not None
        )

    @classmethod
    def _extract_matches(cls, pattern: str, content: str) -> list[str]:
        """Extract stable, displayable candidates without crossing JSON/text boundaries.

        Scans for the declared pattern AND the generic flag shape, because a
        challenge whose banner was configured wrong would otherwise never surface
        the real flag as a candidate at all.
        """
        compiled: list[re.Pattern[str]] = []
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            pass
        compiled.append(GENERIC_FLAG_RE)

        matches: list[str] = []
        for regex in compiled:
            for match in regex.finditer(content):
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

    async def materialize_completion_gate_flag(
        self, session: AsyncSession, run: SolveRun
    ) -> FlagCandidate | None:
        """Persist a Completion-Gate-verified flag as a real FlagCandidate.

        The native Muteki path decides success from its own SharedGraph gate and
        writes ``report_json = {"flag": ..., "flag_verified": True}``.  No tool
        gateway turn runs afterwards, so unlike the legacy solver path nothing
        ever inserted the row the Result panel reads.  The Run therefore looked
        solved (``COMPLETED_SOLVED``, ``flag_verified``) while the Flag-Candidate
        list stayed empty and every candidate-derived surface — review buttons,
        the VALID count, the canonical Blackboard flag — saw nothing.

        The gate is authoritative, so the candidate lands as ``VALID``: this is a
        projection of an already-verified result, not a new claim.  It is derived
        strictly from the durable report, never carved out of prose, and a
        placeholder body is refused so a templated ``flag{...}`` cannot be
        laundered into a solved Run.

        Safe on a read path: when the candidate already exists this is one
        indexed lookup that changes nothing.
        """

        if str(run.solver_mode or "").lower() != "muteki":
            return None

        report = run.report_json if isinstance(run.report_json, dict) else {}
        if not report.get("flag_verified"):
            return None
        candidate = str(report.get("flag") or "").strip()
        if not candidate or is_placeholder_flag(candidate):
            return None

        existing = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
            )
        )
        if existing is not None:
            # A prior review decision is the operator's; only upgrade a row that
            # was never judged, and never silently resurrect one they rejected.
            if existing.review_state == "OPEN":
                existing.verified = True
                existing.review_state = "VALID"
                await session.commit()
                await session.refresh(existing)
            return existing

        challenge = await session.get(Challenge, run.challenge_id)
        pattern = str(getattr(challenge, "flag_pattern", "") or "")
        item = FlagCandidate(
            run_id=run.id,
            candidate=candidate,
            pattern_matched=_matches_declared(candidate, pattern) if pattern else False,
            verified=True,
            review_state="VALID",
            first_seen_source_type="COMPLETION_GATE",
            first_seen_source_id=None,
            first_seen_at=datetime.now(UTC),
            source_assistance_level=run.assistance_level or "AUTONOMOUS",
        )
        session.add(item)
        await session.flush()
        session.add(
            FlagProvenance(
                run_id=run.id,
                candidate_id=item.id,
                first_seen_source_type="COMPLETION_GATE",
                first_seen_source_id=None,
                first_seen_at=item.first_seen_at or datetime.now(UTC),
                verification_source_type="COMPLETION_GATE",
                source_assistance_level=run.assistance_level or "AUTONOMOUS",
                source_is_autonomous=(run.assistance_level or "AUTONOMOUS")
                in {"AUTONOMOUS", "HINT_GUIDED"},
            )
        )
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "flag.candidate_found",
            {"candidate_id": item.id, "source": "COMPLETION_GATE"},
        )
        return item

    async def materialize_suspected_flags(
        self, session: AsyncSession, run: SolveRun
    ) -> list[FlagCandidate]:
        """Persist the Run's wrong-banner flag values as candidates for review.

        A Run can record a flag-shaped value that does not fit the challenge's
        declared banner.  That is not a solve -- the usual cause is a value
        recovered from somewhere other than the target (a public writeup, the
        original repository, the run's own notes) -- so it must never be
        submitted automatically.  Dropping it would lose the only evidence that
        it happened, so it is filed as an ``OPEN`` candidate: the operator sees
        it, and only the operator can promote it to ``VALID``, which is what a
        submission requires.

        Placeholder bodies are refused for the same reason
        ``materialize_completion_gate_flag`` refuses them: a templated
        ``flag{...}`` must not become a reviewable artefact noise.
        """

        if str(run.solver_mode or "").lower() != "muteki":
            return []
        report = run.report_json if isinstance(run.report_json, dict) else {}
        values = report.get("suspected_flags")
        if not isinstance(values, list):
            return []

        challenge = await session.get(Challenge, run.challenge_id)
        pattern = str(getattr(challenge, "flag_pattern", "") or "")
        created: list[FlagCandidate] = []
        for raw in values:
            candidate = str(raw or "").strip()
            if not candidate or is_placeholder_flag(candidate):
                continue
            existing = await session.scalar(
                select(FlagCandidate).where(
                    FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
                )
            )
            if existing is not None:
                # Never downgrade or overwrite an operator's decision.
                created.append(existing)
                continue
            item = FlagCandidate(
                run_id=run.id,
                candidate=candidate,
                pattern_matched=_matches_declared(candidate, pattern) if pattern else False,
                verified=False,
                review_state="OPEN",
                first_seen_source_type="COMPLETION_GATE_UNVERIFIED",
                first_seen_source_id=None,
                first_seen_at=datetime.now(UTC),
                source_assistance_level=run.assistance_level or "AUTONOMOUS",
            )
            session.add(item)
            await session.flush()
            created.append(item)
            await event_service.append(
                session,
                run.id,
                "flag.candidate_found",
                {"candidate_id": item.id, "source": "COMPLETION_GATE_UNVERIFIED"},
            )
        if created:
            await session.commit()
        return created

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
        # A native Muteki Run verifies its flag through the Completion Gate and
        # records it in ``report_json``, NOT through the tool gateway that used
        # to call ``extract_candidates``.  Materialize it as a real candidate
        # before the status check, or the Run is COMPLETED_SOLVED with an empty
        # Flag-Candidate list: the UI files it under "FLAG EVENT RECORDED" and a
        # later review can never actually validate it.
        await self.materialize_completion_gate_flag(session, run)
        # Wrong-banner values are filed for human review, not for submission.
        # Deliberately after the verified materialisation: a Run can hold both,
        # and the verified one must win the ``VALID`` slot.
        await self.materialize_suspected_flags(session, run)
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
                # Honest provenance: a candidate captured only because it has the
                # generic flag shape is recorded as such, so a wrong banner is
                # visible instead of being laundered into "matched the format".
                pattern_matched=_matches_declared(candidate, challenge.flag_pattern),
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
        declared = _matches_declared(candidate, challenge.flag_pattern)
        valid = matches_flag_format(candidate, challenge.flag_pattern)
        item = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id, FlagCandidate.candidate == candidate
            )
        )
        if item is None:
            item = FlagCandidate(
                run_id=run.id,
                candidate=candidate,
                pattern_matched=declared,
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

    async def record_runtime_flag(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        candidate: str,
        *,
        source_type: str = "MUTEKI_RUNTIME",
    ) -> FlagCandidate | None:
        """Materialise a flag an engine reported, re-verified by the host.

        The engine's own ``verified`` bit is never trusted: the value is checked
        against the Challenge's declared banner here, and a value that does not
        match lands as ``OPEN`` for an operator instead of being treated as a
        solve.

        Why this exists: the Muteki path reported its flag *only* as a
        ``muteki.flag_found`` run event.  Every consumer that reads
        ``flag_candidates`` -- the benchmark evaluator, the UI, the submit path
        -- therefore saw nothing.  Run ``9e095f0c`` reached
        ``COMPLETED_SOLVED`` holding the byte-exact flag and was still scored
        ``INCOMPLETE`` with ``verified_flag_candidates: 0``, which would have
        reported Pass@1 = 0 for an arm that actually solved the case.
        """

        text = str(candidate or "").strip()
        if not text:
            return None
        existing = await session.scalar(
            select(FlagCandidate).where(
                FlagCandidate.run_id == run.id,
                FlagCandidate.candidate == text,
            )
        )
        declared = _matches_declared(text, challenge.flag_pattern)
        valid = matches_flag_format(text, challenge.flag_pattern)
        # ``matches_flag_format`` falls back to the generic ``banner{body}``
        # shape so a misconfigured banner cannot make a real flag
        # unrepresentable.  That fallback must not by itself produce a solve:
        # the platform's declared banner is the contract, and a value that only
        # satisfies the generic shape is exactly the "wrong banner, real
        # looking flag" case that has to be reviewed by a human (OPEN) rather
        # than submitted.
        accepted = valid and declared
        now = datetime.now(UTC)
        if existing is None:
            existing = FlagCandidate(
                run_id=run.id,
                candidate=text,
                verified=accepted,
                pattern_matched=declared,
                review_state="VALID" if accepted else "OPEN",
                first_seen_source_type=source_type,
                first_seen_source_id=run.id,
                first_seen_at=now,
                source_assistance_level=run.assistance_level or "AUTONOMOUS",
            )
            session.add(existing)
            await session.flush()
            session.add(
                FlagProvenance(
                    run_id=run.id,
                    candidate_id=existing.id,
                    first_seen_source_type=source_type,
                    first_seen_source_id=run.id,
                    first_seen_at=now,
                    source_assistance_level=run.assistance_level or "AUTONOMOUS",
                    source_is_autonomous=(run.assistance_level or "AUTONOMOUS")
                    in {"AUTONOMOUS", "HINT_GUIDED"},
                )
            )
        elif accepted and not existing.verified:
            existing.verified = True
            existing.pattern_matched = True
            existing.review_state = "VALID"
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "flag.candidate_found",
            {
                "candidate_id": existing.id,
                "verified": bool(existing.verified),
                "source": source_type,
            },
        )
        return existing


flag_service = FlagService()
