import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import (
    Artifact,
    FlagCandidate,
    Hypothesis,
    Observation,
    RunAttempt,
    SolveRun,
    ToolCall,
)
from app.orchestration.state_machine import TERMINAL, RunStatus
from app.services.events import event_service


class ReproductionStep(BaseModel):
    order: int
    title_zh: str
    purpose_zh: str
    tool_name: str
    normalized_arguments: dict[str, Any]
    manual_method: str
    manual_command: str | None = None
    browser_steps: list[str] = Field(default_factory=list)
    expected_status: int | None = None
    expected_evidence: list[str] = Field(default_factory=list)
    source_tool_call_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)


class SolutionPathExtractor:
    _EXCLUDED_TOOLS = {
        "command_execution",
        "node_repl.js",
        "node_repl",
        "web_search",
        "file_read",
        "file_search",
    }
    _SENSITIVE_KEY = re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie)"
    )

    @classmethod
    def _normalize(cls, value: Any, key: str = "") -> Any:
        if cls._SENSITIVE_KEY.search(key):
            return "{{secret_value}}"
        if isinstance(value, dict):
            return {
                str(k): cls._normalize(v, str(k))
                for k, v in value.items()
                if str(k).lower() not in {"follow_redirects"}
            }
        if isinstance(value, list):
            return [cls._normalize(item, key) for item in value[:50]]
        if isinstance(value, str):
            value = re.sub(r"https?://[^/\s]+", "{{target_url}}", value)
            value = re.sub(r"flag\{[^{}\r\n]*\}", "{{flag_pattern_match}}", value, flags=re.I)
            return value[:2000]
        return value

    @staticmethod
    def _curl_for(call: ToolCall) -> str | None:
        args = call.arguments_json or {}
        method = str(args.get("method") or "GET").upper()
        path = str(args.get("url") or "{{target_url}}")
        path = re.sub(r"https?://[^/\s]+", "{{target_url}}", path)
        query = args.get("query") if isinstance(args.get("query"), dict) else {}
        if query:
            encoded = "&".join(f"{key}={value}" for key, value in query.items())
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}{encoded}"
        headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
        header_args = " ".join(
            f"-H {json.dumps(str(key) + ': ' + str(value), ensure_ascii=False)}"
            for key, value in headers.items()
            if str(key).lower() not in {"authorization", "cookie"}
        )
        body = args.get("json") or args.get("form") or args.get("body")
        data_arg = ""
        if isinstance(body, dict):
            data_arg = " --json " + json.dumps(json.dumps(body, ensure_ascii=False))
        elif isinstance(body, str) and body:
            safe_body = re.sub(r"flag\{[^{}\r\n]*\}", "{{flag_pattern_match}}", body, flags=re.I)
            data_arg = " --data " + json.dumps(safe_body[:2000])
        cookie_args = "-c cookies.txt -b cookies.txt " if call.tool_name == "http_session_request" else ""
        return f"curl -i {cookie_args}-X {method} {header_args}{data_arg} {json.dumps(path, ensure_ascii=False)}".strip()

    async def extract(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge
    ) -> list[ReproductionStep]:
        calls = list(
            (
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run.id)
                    .order_by(ToolCall.created_at)
                )
            ).all()
        )
        steps: list[ReproductionStep] = []
        seen: set[str] = set()
        for call in calls:
            if call.status != "COMPLETED" or call.tool_name in self._EXCLUDED_TOOLS:
                continue
            observation = await session.scalar(
                select(Observation)
                .where(Observation.tool_call_id == call.id)
                .order_by(Observation.created_at.desc())
            )
            if not observation:
                continue
            facts = observation.facts_json or {}
            if not facts.get("ok", True) and call.tool_name not in {
                "http_request",
                "http_session_request",
            }:
                continue
            normalized = self._normalize(call.arguments_json or {})
            signature = json.dumps([call.tool_name, normalized], ensure_ascii=False, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            model_view = facts.get("tool_model_view") if isinstance(facts.get("tool_model_view"), dict) else {}
            extracted = model_view.get("extracted_facts") if isinstance(model_view.get("extracted_facts"), dict) else facts
            status = extracted.get("status_code")
            expected = ["响应中包含与当前假设相符的结构化证据"]
            if status is not None:
                expected.insert(0, f"HTTP 状态码为 {status}")
            if extracted.get("suspected_flags"):
                expected.append("响应中重新出现符合 Flag 正则的候选值")
            source_artifacts = [str(observation.artifact_id)] if observation.artifact_id else []
            title = {
                "http_request": "验证 HTTP 入口",
                "http_session_request": "建立并复用认证会话",
                "http_extract": "提取页面结构化线索",
                "content_discovery": "枚举授权路径",
            }.get(call.tool_name, f"执行 {call.tool_name}")
            steps.append(
                ReproductionStep(
                    order=len(steps) + 1,
                    title_zh=title,
                    purpose_zh=str(
                        (call.arguments_json or {}).get("reason")
                        or "验证当前假设并保留可审计证据"
                    ),
                    tool_name=call.tool_name,
                    normalized_arguments=normalized,
                    manual_method="HTTP 请求" if call.tool_name.startswith("http") else "Runner 工具调用",
                    manual_command=self._curl_for(call),
                    browser_steps=["在授权目标上复现相同请求", "检查状态码、跳转目标、页面特征和 Flag 正则"],
                    expected_status=int(status) if isinstance(status, int) else None,
                    expected_evidence=expected,
                    source_tool_call_ids=[call.id],
                    source_artifact_ids=source_artifacts,
                )
            )
        return steps


class ReproductionPlanner:
    async def plan(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge
    ) -> list[ReproductionStep]:
        return await SolutionPathExtractor().extract(session, run, challenge)


class ReproductionVerifier:
    def verify(
        self, steps: list[ReproductionStep], flags: list[FlagCandidate], challenge: Challenge,
        *, fresh_session_verified: bool = False,
    ) -> dict:
        valid = bool(steps) and fresh_session_verified and any(item.verified and item.review_state == "VALID" for item in flags)
        return {
            "verified": valid,
            "reproducible": valid,
            "step_count": len(steps),
            "verified_flag_count": sum(
                1 for item in flags if item.verified and item.review_state == "VALID"
            ),
            "dynamic_flag_required": True,
            "fresh_session_verified": fresh_session_verified,
            "notes": [] if valid else ["尚未完成全新 Session 自动重放验证"],
        }


class ChineseWriteupRenderer:
    def render(
        self,
        challenge: Challenge,
        run: SolveRun,
        result: str,
        calls: list[ToolCall],
        observations: list[Observation],
        hypotheses: list[Hypothesis],
        flags: list[FlagCandidate],
        steps: list[ReproductionStep],
        failure_reason: str,
    ) -> str:
        verified = [item for item in flags if item.verified and item.review_state == "VALID"]
        lines = [
            f"# {challenge.name}：中文可复现解题报告",
            "",
            "## 题目信息",
            f"- 题目类型：{challenge.challenge_type}",
            "- 授权目标：{{target_url}}",
            f"- 解题引擎：{run.engine_type}",
            f"- 结果：{result}",
            "",
            "## 结论",
            "- 本报告只依据本次 Run 的工具调用、结构化观察和证据文件生成。",
            "- Flag、凭据、Cookie、Token、真实主机和本机路径均以变量或脱敏值表示。",
            "",
            "## 手动复现步骤",
        ]
        if steps:
            for step in steps:
                lines.extend(
                    [
                        f"### {step.order}. {step.title_zh}",
                        f"目的：{step.purpose_zh}",
                        f"工具：`{step.tool_name}`",
                        f"命令：`{step.manual_command}`"
                        if step.manual_command
                        else "操作：通过 Runner Gateway 执行结构化工具调用。",
                        f"预期：{'; '.join(step.expected_evidence)}",
                        "",
                    ]
                )
        else:
            lines.extend(["- 当前 Run 没有可证明结论的成功工具路径，不能标记为可复现。", ""])
        lines.extend(
            [
                "## Flag 验证",
                f"- 已验证候选数量：{len(verified)}",
                "- 复现时必须在全新 Session 中重新获取符合 Flag Pattern 的结果。",
                "",
                "## 失败路径",
                f"- {failure_reason or '无明确失败路径'}",
                "",
                "## 自动化摘要",
                f"- Agent 步数：{run.agent_step_count}",
                f"- 工具调用：{len(calls)}",
                f"- 观察数量：{len(observations)}",
                f"- 假设数量：{len(hypotheses)}",
                "",
                "## 证据清单",
                "- 详见 `final/evidence-manifest.json`。",
                "",
            ]
        )
        return "\n".join(lines)


class ReportGenerationBarrier:
    """Single gate for final reports; report data is never a terminal signal."""

    async def check(self, session: AsyncSession, run: SolveRun) -> dict:
        missing: list[str] = []
        if RunStatus(run.status) not in TERMINAL:
            missing.append("run_terminal")
        flags = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all())
        if RunStatus(run.status) == RunStatus.COMPLETED_SOLVED and not any(item.verified and item.review_state == "VALID" for item in flags):
            missing.append("verified_flag")
        if RunStatus(run.status) == RunStatus.COMPLETED_SOLVED and any(item.review_state == "OPEN" for item in flags):
            missing.append("flag_review")
        if await session.scalar(select(RunAttempt.id).where(RunAttempt.run_id == run.id, RunAttempt.status == "RUNNING")):
            missing.append("attempt_closed")
        if await session.scalar(select(ToolCall.id).where(ToolCall.run_id == run.id, ToolCall.status.in_(["REQUESTED", "STARTED"]))):
            missing.append("key_tool_calls_completed")
        if RunStatus(run.status) == RunStatus.COMPLETED_SOLVED and not run.thread_invalidated:
            missing.append("terminal_generation_frozen")
        return {"allowed": not missing, "missing": missing}


report_generation_barrier = ReportGenerationBarrier()


class ReportService:
    async def generate(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        result: str,
        failure_reason: str = "",
    ) -> Artifact:
        barrier = await report_generation_barrier.check(session, run)
        if not barrier["allowed"]:
            raise ValueError("REPORT_GENERATION_BLOCKED: " + ", ".join(barrier["missing"]))
        await event_service.append(session, run.id, "report.started", {})
        calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at)
                )
            ).all()
        )
        observations = list(
            (
                await session.scalars(
                    select(Observation)
                    .where(Observation.run_id == run.id)
                    .order_by(Observation.created_at)
                )
            ).all()
        )
        hypotheses = list(
            (
                await session.scalars(
                    select(Hypothesis)
                    .where(Hypothesis.run_id == run.id)
                    .order_by(Hypothesis.created_at)
                )
            ).all()
        )
        flags = list(
            (
                await session.scalars(
                    select(FlagCandidate)
                    .where(FlagCandidate.run_id == run.id)
                    .order_by(FlagCandidate.created_at)
                )
            ).all()
        )
        steps = await ReproductionPlanner().plan(session, run, challenge)
        root = Path(run.workspace_path).resolve()
        final = root / "final"
        final.mkdir(parents=True, exist_ok=True)
        verifier = ReproductionVerifier().verify(
            steps, flags, challenge, fresh_session_verified=bool(run.fresh_reproduction_verified)
        )
        manifest = [
            {
                "path": item.file_path,
                "artifact_id": item.id,
                "sha256": item.sha256,
                "type": item.artifact_type,
                "source_tool_call_id": item.tool_call_id,
            }
            for item in await session.scalars(select(Artifact).where(Artifact.run_id == run.id))
        ]
        (final / "reproduction.json").write_text(
            json.dumps({"verified": verifier, "steps": [item.model_dump() for item in steps]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (final / "reproduction-validation.json").write_text(
            json.dumps(verifier, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (final / "evidence-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raw = ChineseWriteupRenderer().render(
            challenge, run, result, calls, observations, hypotheses, flags, steps, failure_reason
        ).encode("utf-8")
        path = final / "writeup.zh-CN.md"
        path.write_bytes(raw)
        (final / "writeup.md").write_bytes(raw)
        artifact = Artifact(
            run_id=run.id,
            artifact_type="report",
            file_path="final/writeup.zh-CN.md",
            mime_type="text/markdown",
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            summary="中文版可复现解题报告",
            status="ACTIVE",
        )
        old_reports = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))).all())
        for old in old_reports:
            old.status = "STALE"
        session.add(artifact)
        await session.commit()
        await event_service.append(
            session, run.id, "artifact.created", {"artifact_id": artifact.id, "path": artifact.file_path}
        )
        await event_service.append(
            session, run.id, "report.completed", {"artifact_id": artifact.id, "reproduction": verifier}
        )
        return artifact


report_service = ReportService()
