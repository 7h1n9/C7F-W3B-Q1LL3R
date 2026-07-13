import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, SolveRun, ToolCall
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
    _EXCLUDED_TOOLS = {"command_execution", "node_repl.js", "node_repl", "web_search", "file_read", "file_search"}
    _SENSITIVE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie)")

    @classmethod
    def _normalize(cls, value: Any, key: str = "") -> Any:
        if cls._SENSITIVE.search(key):
            return "{{discovered_" + key.lower().replace("-", "_") + "}}"
        if isinstance(value, dict):
            return {str(k): cls._normalize(v, str(k)) for k, v in value.items() if str(k).lower() not in {"follow_redirects"}}
        if isinstance(value, list):
            return [cls._normalize(item, key) for item in value[:50]]
        if isinstance(value, str):
            value = re.sub(r"https?://[^/\s]+", "{{target_url}}", value)
            value = re.sub(r"flag\{[^{}\r\n]*\}", "{{flag_pattern_match}}", value, flags=re.I)
            return value[:2000]
        return value

    async def extract(self, session: AsyncSession, run: SolveRun, challenge: Challenge) -> list[ReproductionStep]:
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at))).all())
        steps: list[ReproductionStep] = []
        seen: set[str] = set()
        for call in calls:
            if call.status != "COMPLETED" or call.tool_name in self._EXCLUDED_TOOLS:
                continue
            observation = await session.scalar(select(Observation).where(Observation.tool_call_id == call.id).order_by(Observation.created_at.desc()))
            if not observation:
                continue
            facts = observation.facts_json or {}
            if not facts.get("ok", True) and call.tool_name not in {"http_request", "http_session_request"}:
                continue
            normalized = self._normalize(call.arguments_json or {})
            signature = json.dumps([call.tool_name, normalized], ensure_ascii=False, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            status = facts.get("status_code")
            expected = []
            if status is not None:
                expected.append(f"HTTP 状态码为 {status}")
            expected.append("观察结果中包含与假设相符的结构化证据")
            command = None
            method = str((call.arguments_json or {}).get("method") or "GET").upper()
            if call.tool_name == "http_session_request":
                command = f"curl -i -c cookies.txt -b cookies.txt -X {method} '{{target_url}}'"
            elif call.tool_name in {"http_request", "http_extract"}:
                command = f"curl -i -X {method} '{{target_url}}'"
            source_artifacts = [str(observation.artifact_id)] if observation.artifact_id else []
            steps.append(ReproductionStep(
                order=len(steps) + 1,
                title_zh={"http_request": "验证 HTTP 入口", "http_session_request": "建立并复用认证会话", "http_extract": "提取页面结构化线索"}.get(call.tool_name, f"执行 {call.tool_name}"),
                purpose_zh=str(call.arguments_json.get("reason") or "验证当前假设并保留可审计证据"),
                tool_name=call.tool_name,
                normalized_arguments=normalized,
                manual_method="HTTP 请求" if call.tool_name.startswith("http") else "Runner 工具调用",
                manual_command=command,
                browser_steps=[f"在授权目标上执行 {method} 请求", "检查响应状态、跳转目标和页面特征"],
                expected_status=int(status) if isinstance(status, int) else None,
                expected_evidence=expected,
                source_tool_call_ids=[call.id],
                source_artifact_ids=source_artifacts,
            ))
        return steps


class ReproductionPlanner:
    async def plan(self, session: AsyncSession, run: SolveRun, challenge: Challenge) -> list[ReproductionStep]:
        return await SolutionPathExtractor().extract(session, run, challenge)


class ReproductionVerifier:
    def verify(self, steps: list[ReproductionStep], flags: list[FlagCandidate], challenge: Challenge) -> dict:
        valid = bool(steps) and any(item.verified and item.review_state == "VALID" for item in flags)
        return {"verified": valid, "step_count": len(steps), "verified_flag_count": sum(1 for item in flags if item.verified and item.review_state == "VALID"), "dynamic_flag_required": True, "notes": [] if valid else ["尚未完成全新 Session 下的人工复现验证"]}


class ChineseWriteupRenderer:
    def render(self, challenge: Challenge, run: SolveRun, result: str, calls: list[ToolCall], observations: list[Observation], hypotheses: list[Hypothesis], flags: list[FlagCandidate], steps: list[ReproductionStep], failure_reason: str) -> str:
        verified = [item for item in flags if item.verified and item.review_state == "VALID"]
        lines = [f"# {challenge.name}：中文可复现解题报告", "", "## 题目信息", f"- 题目类型：{challenge.challenge_type}", "- 授权目标：{target_url}", f"- 解题引擎：{run.engine_type}", "", "## 考点与漏洞结论", "- 结论仅依据本 Run 的结构化观察、工具调用和证据文件，不复制其他 Run。", "- Flag 使用配置的正则进行验证，复现时必须重新获取动态值。", "", "## 解题思路概述"]
        lines.append("- 按入口识别、基线响应、假设验证、会话/权限边界和 Flag 验证的顺序推进。")
        lines.extend(["", "## 环境与前置条件", "- 仅在题目配置的允许主机范围内执行。", "- Runner 负责 HTTP、文件和分析工具；Cookie 只保存在 Runner 的隔离 SessionStore。", "", "## 手动复现步骤"])
        if steps:
            for step in steps:
                lines.extend([f"### {step.order}. {step.title_zh}", f"目的：{step.purpose_zh}", f"工具：`{step.tool_name}`", f"命令：`{step.manual_command}`" if step.manual_command else "操作：通过 Runner Gateway 执行结构化工具调用", f"预期：{'; '.join(step.expected_evidence)}"])
        else:
            lines.append("- 当前 Run 没有可证明结论的成功工具路径，不能标记为可复现。")
        completed_calls = [f"- `{item.tool_name}`：{item.status}" for item in calls if item.status == "COMPLETED" and item.tool_name not in SolutionPathExtractor._EXCLUDED_TOOLS] or ["- 无"]
        lines.extend(["", "## 关键请求与响应", *completed_calls, "", "## Flag 验证", f"- 已验证候选数量：{len(verified)}", "- 复现要求：在全新 Session 中重新取得与 Flag Pattern 匹配的值；不把历史 Flag 写死在命令或 Skill 中。", "", "## 失败路径", f"- {failure_reason or '无明确失败路径'}", "", "## 修复建议", "- 对 30x 跳转按浏览器语义处理，并逐跳重验授权主机。", "- 对限流、配额和结构化输出错误分类记录，避免把 Provider 错误误报为 Action 解析失败。", "", "## 自动化执行摘要", f"- Agent 步数：{run.agent_step_count}", f"- 工具调用：{len(calls)}", f"- 观察数量：{len(observations)}", f"- 假设数量：{len(hypotheses)}", "", "## 证据清单", "- 详见 `final/evidence-manifest.json`。", ""])
        return "\n".join(lines)


class ReportService:
    async def generate(self, session: AsyncSession, run: SolveRun, challenge: Challenge, result: str, failure_reason: str = "") -> Artifact:
        await event_service.append(session, run.id, "report.started", {})
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at))).all())
        observations = list((await session.scalars(select(Observation).where(Observation.run_id == run.id).order_by(Observation.created_at))).all())
        hypotheses = list((await session.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id).order_by(Hypothesis.created_at))).all())
        flags = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id).order_by(FlagCandidate.created_at))).all())
        steps = await ReproductionPlanner().plan(session, run, challenge)
        root = Path(run.workspace_path).resolve()
        final = root / "final"
        final.mkdir(parents=True, exist_ok=True)
        verifier = ReproductionVerifier().verify(steps, flags, challenge)
        manifest = [{"path": item.file_path, "artifact_id": item.id, "sha256": item.sha256, "type": item.artifact_type, "source_tool_call_id": item.tool_call_id} for item in await session.scalars(select(Artifact).where(Artifact.run_id == run.id))]
        (final / "reproduction.json").write_text(json.dumps({"verified": verifier, "steps": [item.model_dump() for item in steps]}, ensure_ascii=False, indent=2), encoding="utf-8")
        (final / "evidence-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        raw = ChineseWriteupRenderer().render(challenge, run, result, calls, observations, hypotheses, flags, steps, failure_reason).encode()
        path = final / "writeup.zh-CN.md"
        path.write_bytes(raw)
        legacy = final / "writeup.md"
        legacy.write_bytes(raw)
        artifact = Artifact(run_id=run.id, artifact_type="report", file_path="final/writeup.zh-CN.md", mime_type="text/markdown", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), summary="中文版可复现解题报告")
        session.add(artifact)
        await session.commit()
        await event_service.append(session, run.id, "artifact.created", {"artifact_id": artifact.id, "path": artifact.file_path})
        await event_service.append(session, run.id, "report.completed", {"artifact_id": artifact.id, "reproduction": verifier})
        return artifact


report_service = ReportService()
