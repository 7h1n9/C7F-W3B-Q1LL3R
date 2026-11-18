import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.knowledge.loader import KnowledgeLoader, load_knowledge_items
from app.knowledge.models import KnowledgeItem, KnowledgeProvenance
from app.models.challenge import Challenge
from app.models.learned_skill import (
    LearnedSkillCandidate,
    LearnedSkillCandidateSource,
    LearnedSkillReview,
)
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, SolveRun, ToolCall
from app.services.reports import ReproductionPlanner

# Published learned skills live in their own file so the three hand-curated
# domain files stay byte-identical and a reviewer can always tell at a glance
# which entries were derived from this project's own runs.
LEARNED_CORPUS_FILENAME = "learned.jsonl"
_VERIFIED_STEPS_HEADING = "## 已验证步骤"


@dataclass(frozen=True)
class PublicationCuration:
    """The fields a reviewer must supply when publishing a candidate.

    ``create_from_run`` produces a narrative markdown blob, but a corpus entry
    needs structured claims.  ``steps`` can be recovered from the blob because
    the generator writes them under a known heading; ``known_failures`` and
    ``verification_method`` cannot be recovered from anything, so they are
    required here.  The loader rejects a ``case`` entry without
    ``known_failures``, and rejects any entry without ``steps`` or
    ``verification_method``, so a publication that cannot fill them fails
    loudly instead of writing a corpus line that breaks the next load.
    """

    technique: str
    known_failures: tuple[str, ...] = ()
    verification_method: tuple[str, ...] = ()
    target_stack: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    title: str | None = None
    summary: str | None = None


def _steps_from_markdown(text: str) -> list[str]:
    """Recover the numbered verified steps the generator wrote into the blob."""

    lines = str(text or "").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == _VERIFIED_STEPS_HEADING),
        None,
    )
    if start is None:
        return []
    steps: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if stripped:
            steps.append(stripped)
    return steps


def _clean(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return [str(value).strip() for value in values if str(value or "").strip()]


class SensitiveValueSanitizer:
    SECRET_KV = re.compile(
        r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization|cookie|username)\b"
        # Bearer first: the generic `[:=]` alternative would otherwise swallow
        # the word "Bearer" as the secret and leak the actual token after it.
        r"(:\s*Bearer\s+|\s*[:=]\s*)([^\s,;\"']+)"
    )
    FLAG = re.compile(r"flag\{[^{}\r\n]*\}", re.I)
    URL = re.compile(r"https?://[^\s)\"']+")
    IP_PORT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
    WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+")
    LINUX_PATH = re.compile(r"(?<![\w])/(?:home|tmp|var|opt|app|etc|root)/[^\s\"']+")
    UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

    @classmethod
    def sanitize(cls, text: str) -> tuple[str, dict]:
        counters = {
            "secret_kv": 0,
            "flag": 0,
            "url": 0,
            "ip_host": 0,
            "windows_path": 0,
            "linux_path": 0,
            "uuid": 0,
        }

        def count(name: str, replacement: str):
            def repl(_: re.Match) -> str:
                counters[name] += 1
                return replacement

            return repl

        value = cls.SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{{{{secret_value}}}}", text)
        counters["secret_kv"] = len(cls.SECRET_KV.findall(text))
        value = cls.FLAG.sub(count("flag", "{{flag_pattern}}"), value)
        value = cls.URL.sub(count("url", "{{target_url}}"), value)
        value = cls.IP_PORT.sub(count("ip_host", "{{target_host}}"), value)
        value = cls.WINDOWS_PATH.sub(count("windows_path", "{{windows_path}}"), value)
        value = cls.LINUX_PATH.sub(count("linux_path", "{{linux_path}}"), value)
        value = cls.UUID.sub(count("uuid", "{{id}}"), value)
        scan = {
            "redaction_count": sum(counters.values()),
            "redaction_types": {key: count for key, count in counters.items() if count},
            "passed": not any(
                regex.search(value)
                for regex in (cls.FLAG, cls.IP_PORT, cls.WINDOWS_PATH, cls.UUID)
            ),
        }
        return value, scan


class LearnedSkillService:
    _PLATFORM_TOOLS = {"command_execution", "node_repl.js", "node_repl", "shell", "exec"}

    async def create_from_run(self, session: AsyncSession, run_id: str) -> LearnedSkillCandidate:
        run = await session.get(SolveRun, run_id)
        if not run:
            raise DomainError("RUN_NOT_FOUND", "Run not found.", status_code=404)
        if run.status != "COMPLETED_SOLVED":
            raise DomainError(
                "LEARN_RUN_NOT_SOLVED",
                "Only a verified solved Run can produce a Skill candidate.",
                status_code=422,
            )
        challenge = await session.get(Challenge, run.challenge_id)
        if not challenge:
            raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
        flags = list(
            (
                await session.scalars(
                    select(FlagCandidate).where(FlagCandidate.run_id == run_id)
                )
            ).all()
        )
        if not any(item.verified and item.review_state == "VALID" for item in flags):
            raise DomainError("LEARN_FLAG_NOT_VERIFIED", "A verified Flag is required.", status_code=422)
        artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all())
        if not artifacts:
            raise DomainError("LEARN_NO_ARTIFACT", "A source artifact is required.", status_code=422)
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id))).all())
        violations = sorted({call.tool_name for call in calls if call.tool_name in self._PLATFORM_TOOLS})
        if violations:
            raise DomainError(
                "LEARN_UNSAFE_RUN",
                "Run contains disallowed platform/debug behavior.",
                {"tools": violations},
                422,
            )
        observations = list(
            (await session.scalars(select(Observation).where(Observation.run_id == run_id))).all()
        )
        hypotheses = list(
            (await session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id))).all()
        )
        steps = await ReproductionPlanner().plan(session, run, challenge)
        if not steps:
            raise DomainError(
                "LEARN_REPRODUCTION_REQUIRED",
                "A reproducible step path is required before creating a Skill candidate.",
                status_code=422,
            )
        raw = "\n".join(
            [
                f"# {challenge.name} 通用解题经验",
                "",
                "适用范围：授权 Web CTF；先建立基线，再验证结构化线索、会话边界和 Flag 证据。",
                "",
                "## 已验证步骤",
                *[f"{step.order}. {step.title_zh}：{step.purpose_zh}" for step in steps],
                "",
                "## 安全约束",
                "仅通过 Tool Gateway 执行工具；不包含任意命令、真实凭据、Flag、目标 IP、Run ID 或本机路径。",
            ]
        )
        sanitized, scan = SensitiveValueSanitizer.sanitize(raw)
        if not scan["passed"]:
            status = "QUARANTINED"
        else:
            status = "REVIEW_REQUIRED"
        slug = re.sub(r"[^a-z0-9]+", "-", challenge.name.lower()).strip("-")[:60] or "verified-path"
        name = f"learned-{slug}"
        suffix = 2
        while await session.scalar(select(LearnedSkillCandidate.id).where(LearnedSkillCandidate.name == name)):
            name = f"learned-{slug}-{suffix}"
            suffix += 1
        candidate = LearnedSkillCandidate(
            name=name,
            display_name=f"待审核：{challenge.name}",
            description="从成功 Run 的可复现路径生成，等待人工审核后才能发布为正式 Skill。",
            status=status,
            content_markdown=raw,
            sanitized_content=sanitized,
            source_run_id=run_id,
            source_artifact_ids=[item.id for item in artifacts],
            source_observation_ids=[item.id for item in observations],
            metadata_json={
                "challenge_type": challenge.challenge_type,
                "hypothesis_count": len(hypotheses),
                "step_count": len(steps),
                "require_human_review": True,
            },
            security_scan_json=scan,
            generalization_score=70 if steps else 0,
        )
        session.add(candidate)
        await session.flush()
        for source_type, ids in (
            ("artifact", candidate.source_artifact_ids),
            ("observation", candidate.source_observation_ids),
        ):
            for source_id in ids:
                session.add(
                    LearnedSkillCandidateSource(
                        candidate_id=candidate.id,
                        source_type=source_type,
                        source_id=source_id,
                        detail_json={},
                    )
                )
        await session.commit()
        await session.refresh(candidate)
        return candidate

    async def review(
        self,
        session: AsyncSession,
        candidate_id: str,
        decision: str,
        review: dict | None = None,
        reviewer: str = "human",
    ) -> LearnedSkillCandidate:
        candidate = await session.get(LearnedSkillCandidate, candidate_id)
        if not candidate:
            raise DomainError("LEARN_CANDIDATE_NOT_FOUND", "Skill candidate not found.", status_code=404)
        if decision not in {"APPROVE", "REVISE", "REJECT"}:
            raise DomainError("LEARN_REVIEW_INVALID", "Unsupported review decision.", status_code=422)
        candidate.status = {"APPROVE": "APPROVED", "REVISE": "REVIEW_REQUIRED", "REJECT": "REJECTED"}[decision]
        session.add(
            LearnedSkillReview(
                candidate_id=candidate.id,
                decision=decision,
                reviewer=reviewer,
                review_json=review or {},
            )
        )
        await session.commit()
        await session.refresh(candidate)
        return candidate

    async def publish(
        self,
        session: AsyncSession,
        candidate_id: str,
        curation: PublicationCuration,
        *,
        root: str | Path | None = None,
    ) -> KnowledgeItem:
        """Write an approved candidate into the recall corpus as a ``case`` item.

        This is the second half of human review: :meth:`review` records the
        decision, ``publish`` makes the reviewed content recallable as a PRIOR.
        Publication is fail-closed — the whole corpus is re-validated through the
        same loader the retriever uses, and the append is rolled back if it does
        not load, so a bad publication can never poison retrieval.

        ``published_at`` is the source run's own completion date.  That is not a
        placeholder: it is the date the knowledge was actually produced, which is
        exactly the semantic a time-held-out experiment needs.  Automatically
        distilled CAP-32 templates stay undated and are excluded from such a
        split.
        """

        candidate = await session.get(LearnedSkillCandidate, candidate_id)
        if not candidate:
            raise DomainError(
                "LEARN_CANDIDATE_NOT_FOUND", "Skill candidate not found.", status_code=404
            )
        if candidate.status != "APPROVED":
            raise DomainError(
                "LEARN_CANDIDATE_NOT_APPROVED",
                "Only an APPROVED candidate can be published.",
                {"status": candidate.status},
                422,
            )
        if not (candidate.security_scan_json or {}).get("passed"):
            raise DomainError(
                "LEARN_SCAN_NOT_PASSED",
                "The candidate failed its secret scan and cannot be published.",
                status_code=422,
            )

        run = await session.get(SolveRun, candidate.source_run_id)
        if not run:
            raise DomainError("RUN_NOT_FOUND", "Source run not found.", status_code=404)
        challenge = await session.get(Challenge, run.challenge_id)

        known_failures = _clean(curation.known_failures)
        if not known_failures:
            raise DomainError(
                "LEARN_KNOWN_FAILURES_REQUIRED",
                "A case entry must name the conditions under which the route stops working.",
                status_code=422,
            )
        verification_method = _clean(curation.verification_method)
        if not verification_method:
            raise DomainError(
                "LEARN_VERIFICATION_REQUIRED",
                "A published skill needs at least one verification step.",
                status_code=422,
            )
        steps = _clean(curation.steps) or _steps_from_markdown(candidate.sanitized_content)
        if not steps:
            raise DomainError(
                "LEARN_STEPS_REQUIRED",
                "Could not recover executable steps for the entry.",
                status_code=422,
            )
        technique = str(curation.technique or "").strip()
        if not technique:
            raise DomainError(
                "LEARN_TECHNIQUE_REQUIRED",
                "A published skill needs a technique label to be retrievable.",
                status_code=422,
            )

        produced_at = run.finished_at or run.created_at
        if produced_at is None:
            raise DomainError(
                "LEARN_PUBLISHED_AT_UNKNOWN",
                "The source run has no completion timestamp to date the entry by.",
                status_code=422,
            )

        # Every free-text field is redacted, because the corpus is a recall
        # channel: nothing in it may carry a real flag, host, path or secret.
        redactions = 0

        def _redact(value: object) -> str:
            nonlocal redactions
            cleaned, scan = SensitiveValueSanitizer.sanitize(str(value))
            redactions += int(scan["redaction_count"])
            return cleaned

        fields = {
            "title": _redact(
                (curation.title or "").strip()
                or f"{(challenge.name if challenge else 'Verified target')} verified path"
            ),
            "summary": _redact((curation.summary or "").strip() or candidate.description),
            "technique": _redact(technique),
            "preconditions": [_redact(value) for value in _clean(curation.preconditions)],
            "target_stack": [_redact(value) for value in _clean(curation.target_stack)],
            "verification_method": [_redact(value) for value in verification_method],
            "known_failures": [_redact(value) for value in known_failures],
            "steps": [_redact(value) for value in steps],
        }

        target_root = self._corpus_root(root)
        path = target_root / LEARNED_CORPUS_FILENAME
        item_id = self._knowledge_id(candidate.name)
        if item_id in self._existing_ids(target_root):
            raise DomainError(
                "LEARN_ALREADY_PUBLISHED",
                "This candidate already has an entry in the learned corpus.",
                {"knowledge_id": item_id},
                409,
            )

        item = KnowledgeItem(
            id=item_id,
            kind="case",
            title=fields["title"],
            technique=fields["technique"],
            summary=fields["summary"],
            preconditions=fields["preconditions"],
            target_stack=fields["target_stack"],
            payload_template=None,
            verification_method=fields["verification_method"],
            known_failures=fields["known_failures"],
            steps=fields["steps"],
            provenance=KnowledgeProvenance(
                source_type="learned",
                source_ref=(
                    f"VeriSure learned skill {candidate.name} "
                    f"(source run {candidate.source_run_id})"
                ),
                source_url=None,
                notes=(
                    "Human-reviewed publication derived from a verified solved run; "
                    f"{redactions} value(s) redacted before writing."
                ),
            ),
            published_at=produced_at.date(),
            trust_level="curated",
        )

        original = path.read_text(encoding="utf-8") if path.exists() else None
        line = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
        path.write_text((original or "") + line + "\n", encoding="utf-8")
        try:
            KnowledgeLoader(target_root).load()
        except Exception as exc:  # noqa: BLE001 - any loader rejection rolls back
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")
            raise DomainError(
                "LEARN_PUBLICATION_INVALID",
                f"The published entry was rejected by the corpus loader: {exc}",
                {"knowledge_id": item_id},
                422,
            ) from exc

        # The BM25 index is process-cached, so a stale index would hide the new
        # entry until restart.
        from app.services.knowledge_context import clear_retriever_cache

        clear_retriever_cache()

        metadata = dict(candidate.metadata_json or {})
        metadata["published_knowledge_id"] = item.id
        metadata["published_at"] = item.published_at.isoformat() if item.published_at else None
        metadata["published_corpus"] = str(path)
        candidate.metadata_json = metadata
        await session.commit()
        await session.refresh(candidate)
        return item

    @staticmethod
    def _corpus_root(root: str | Path | None) -> Path:
        if root is None:
            from app.knowledge.config import load_knowledge_config

            root = load_knowledge_config().root
        resolved = Path(root).expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    @staticmethod
    def _knowledge_id(candidate_name: str) -> str:
        slug = str(candidate_name or "")
        if slug.startswith("learned-"):
            slug = slug[len("learned-"):]
        slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "verified-path"
        return f"learned-{slug}"

    @staticmethod
    def _existing_ids(root: Path) -> set[str]:
        """Ids already present in the corpus, or none if it does not load yet."""

        try:
            return {item.id for item in load_knowledge_items(root)}
        except (FileNotFoundError, ValueError):
            return set()


learned_skill_service = LearnedSkillService()