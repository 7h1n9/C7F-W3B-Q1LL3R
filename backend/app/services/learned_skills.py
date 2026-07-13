import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.learned_skill import (
    LearnedSkillCandidate,
    LearnedSkillCandidateSource,
    LearnedSkillReview,
)
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, SolveRun, ToolCall
from app.services.reports import ReproductionPlanner


class SensitiveValueSanitizer:
    SECRET_KV = re.compile(
        r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization|cookie|username)\b"
        r"(\s*[:=]\s*|:\s*Bearer\s+)([^\s,;\"']+)"
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


learned_skill_service = LearnedSkillService()
