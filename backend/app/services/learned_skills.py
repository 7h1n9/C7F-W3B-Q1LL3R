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


class LearnedSkillService:
    _SECRET = re.compile(r"(?i)(authorization|cookie|set-cookie|token|password|passwd|secret|api[_-]?key|flag\{[^}]+\})")
    _PLATFORM_TOOLS = {"command_execution", "node_repl.js", "node_repl", "shell", "exec"}

    @classmethod
    def sanitize(cls, text: str) -> tuple[str, dict]:
        hits = sorted(set(cls._SECRET.findall(text)))
        value = cls._SECRET.sub("{{redacted}}", text)
        value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}:?\d*\b", "{{target_host}}", value)
        value = re.sub(r"(?i)(run|challenge|artifact|observation)[-_ ]?id\s*[:=]\s*[0-9a-f-]{8,}", r"\1_id: {{id}}", value)
        return value, {"redaction_count": len(hits), "redaction_types": hits}

    async def create_from_run(self, session: AsyncSession, run_id: str) -> LearnedSkillCandidate:
        run = await session.get(SolveRun, run_id)
        if not run:
            raise DomainError("RUN_NOT_FOUND", "Run not found.", status_code=404)
        if run.status != "COMPLETED_SOLVED":
            raise DomainError("LEARN_RUN_NOT_SOLVED", "Only a verified solved Run can produce a Skill candidate.", status_code=422)
        challenge = await session.get(Challenge, run.challenge_id)
        flags = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run_id))).all())
        if not any(item.verified and item.review_state == "VALID" for item in flags):
            raise DomainError("LEARN_FLAG_NOT_VERIFIED", "A verified Flag is required.", status_code=422)
        artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all())
        if not artifacts:
            raise DomainError("LEARN_NO_ARTIFACT", "A source artifact is required.", status_code=422)
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id))).all())
        violations = sorted({call.tool_name for call in calls if call.tool_name in self._PLATFORM_TOOLS or "localhost" in str(call.arguments_json).lower() and call.tool_name not in {"http_request", "http_session_request", "http_extract"}})
        if violations:
            raise DomainError("LEARN_UNSAFE_RUN", "Run contains disallowed platform/debug behavior.", {"tools": violations}, 422)
        observations = list((await session.scalars(select(Observation).where(Observation.run_id == run_id))).all())
        hypotheses = list((await session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id))).all())
        steps = await ReproductionPlanner().plan(session, run, challenge)
        raw = "\n".join([f"# {challenge.name} 通用解题经验", "", "适用范围：授权 Web CTF；先建立基线，再验证结构化线索和会话边界。", "", "## 已验证步骤", *[f"{step.order}. {step.title_zh}：{step.purpose_zh}" for step in steps], "", "## 安全约束", "仅通过 Tool Gateway 执行工具；不包含任意命令、真实凭据、Flag、目标 IP、Run ID 或本机路径。"])
        sanitized, scan = self.sanitize(raw)
        slug = re.sub(r"[^a-z0-9]+", "-", challenge.name.lower()).strip("-")[:60] or "verified-path"
        name = f"learned-{slug}"
        suffix = 2
        while await session.scalar(select(LearnedSkillCandidate.id).where(LearnedSkillCandidate.name == name)):
            name = f"learned-{slug}-{suffix}"; suffix += 1
        candidate = LearnedSkillCandidate(name=name, display_name=f"已验证：{challenge.name}", description="从成功 Run 的可复现路径生成，待隔离审核。", status="QUARANTINED", content_markdown=raw, sanitized_content=sanitized, source_run_id=run_id, source_artifact_ids=[item.id for item in artifacts], source_observation_ids=[item.id for item in observations], metadata_json={"challenge_type": challenge.challenge_type, "hypothesis_count": len(hypotheses), "step_count": len(steps)}, security_scan_json={**scan, "passed": True}, generalization_score=70 if steps else 0)
        session.add(candidate); await session.flush()
        for source_type, ids in (("artifact", candidate.source_artifact_ids), ("observation", candidate.source_observation_ids)):
            for source_id in ids:
                session.add(LearnedSkillCandidateSource(candidate_id=candidate.id, source_type=source_type, source_id=source_id, detail_json={}))
        await session.commit(); await session.refresh(candidate)
        return candidate

    async def review(self, session: AsyncSession, candidate_id: str, decision: str, review: dict | None = None, reviewer: str = "human") -> LearnedSkillCandidate:
        candidate = await session.get(LearnedSkillCandidate, candidate_id)
        if not candidate:
            raise DomainError("LEARN_CANDIDATE_NOT_FOUND", "Skill candidate not found.", status_code=404)
        if decision not in {"APPROVE", "REVISE", "REJECT"}:
            raise DomainError("LEARN_REVIEW_INVALID", "Unsupported review decision.", status_code=422)
        candidate.status = {"APPROVE": "APPROVED", "REVISE": "REVIEW_REQUIRED", "REJECT": "REJECTED"}[decision]
        session.add(LearnedSkillReview(candidate_id=candidate.id, decision=decision, reviewer=reviewer, review_json=review or {}))
        await session.commit(); await session.refresh(candidate)
        return candidate


learned_skill_service = LearnedSkillService()
