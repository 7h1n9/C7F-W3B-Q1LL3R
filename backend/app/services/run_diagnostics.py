import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import Artifact, FlagCandidate, Observation, RunEvent, SolveRun, ToolCall
from app.models.skill import Skill
from app.orchestration.state_machine import RunStatus
from app.services.events import event_service
from app.services.solver_state import solver_state_service


def _blob(*values: object) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        else:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
    return "\n".join(parts).lower()


def _append_anomaly(
    items: list[dict],
    *,
    code: str,
    severity: str,
    title: str,
    summary: str,
    evidence: list[str],
    suggestion: str,
) -> None:
    items.append(
        {
            "code": code,
            "severity": severity,
            "title": title,
            "summary": summary,
            "evidence": evidence,
            "suggestion": suggestion,
        }
    )


class RunDiagnosticsService:
    async def _active_skill_names(self, session: AsyncSession, state) -> list[str]:
        if not state or not (state.active_skill_ids_json or []):
            return []
        skills = list(
            (
                await session.scalars(select(Skill).where(Skill.id.in_(state.active_skill_ids_json)))
            ).all()
        )
        return [skill.display_name or skill.name for skill in skills]

    async def analyze(self, session: AsyncSession, run: SolveRun) -> dict:
        challenge = await session.get(Challenge, run.challenge_id)
        state = await solver_state_service.load(session, run.id)
        model = await session.get(ModelConfig, run.model_config_id) if run.model_config_id else None
        events = await event_service.history(session, run.id)
        tool_calls = list(
            (
                await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id))
            ).all()
        )
        observations = list(
            (
                await session.scalars(select(Observation).where(Observation.run_id == run.id))
            ).all()
        )
        artifacts = list(
            (
                await session.scalars(select(Artifact).where(Artifact.run_id == run.id))
            ).all()
        )
        flag_candidates = list(
            (
                await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))
            ).all()
        )

        evidence_blob = _blob(
            run.last_error_code,
            run.last_error_message,
            [event.payload_json for event in events],
            [call.arguments_json for call in tool_calls],
            [obs.summary for obs in observations],
            [obs.facts_json for obs in observations],
            [artifact.summary for artifact in artifacts],
            [artifact.file_path for artifact in artifacts],
        )
        anomalies: list[dict] = []

        if "python_run only accepts existing scripts" in evidence_blob:
            _append_anomaly(
                anomalies,
                code="TOOL_CONTRACT_MISMATCH",
                severity="high",
                title="工具契约不匹配",
                summary="运行记录显示 agent 试图直接把字符串命令交给 python_run，但该工具只接受仓库内已存在的 scripts/*.py 文件。",
                evidence=["python_run only accepts existing scripts/*.py files"],
                suggestion="改为先把逻辑落地成 scripts/*.py 文件再调用 python_run，或切换到适合一次性命令执行的工具。",
            )

        redirect_hits = evidence_blob.count("/profile") + evidence_blob.count("/admin") + evidence_blob.count("302 /login")
        if redirect_hits >= 4 and "login" in evidence_blob:
            evidence = []
            for candidate in ["/profile", "/admin", "302 /login"]:
                if candidate in evidence_blob:
                    evidence.append(candidate)
            _append_anomaly(
                anomalies,
                code="AUTH_REDIRECT_LOOP",
                severity="high",
                title="认证边界被反复撞击",
                summary="记录里出现多次 /profile、/admin 以及 302 /login 的反复试探，说明 agent 在认证边界外循环。",
                evidence=evidence or ["/profile", "/admin", "302 /login"],
                suggestion="先确认授权态的会话/Token 形态，再在允许范围内做一次最小验证，避免在登录重定向上重复试探。",
            )

        no_progress_count = state.no_progress_count if state else 0
        rejected_paths = (state.rejected_paths_json if state else []) or []
        skill_rejections = [
            item
            for item in rejected_paths
            if str(item.get("error_code") or item.get("code") or "").startswith("SKILL_")
        ]
        if no_progress_count >= 2 or skill_rejections:
            _append_anomaly(
                anomalies,
                code="METHOD_LOOPS",
                severity="medium",
                title="方法论陷入重复循环",
                summary="Solver State 中已经出现连续无进展或技能拒绝，说明当前路径需要切换到更低风险的验证步骤。",
                evidence=[
                    f"no_progress_count={no_progress_count}",
                    *(str(item.get("error_code") or item.get("code")) for item in skill_rejections[:3]),
                ],
                suggestion="回退到当前阶段的最小证据集：先激活必要方法论技能，再做一次最小化验证，避免继续重复同类动作。",
            )

        if run.status == RunStatus.FAILED_ENGINE.value and flag_candidates:
            valid_flags = [item.candidate for item in flag_candidates if item.review_state == "VALID"]
            open_flags = [item.candidate for item in flag_candidates if item.review_state == "OPEN"]
            if valid_flags:
                _append_anomaly(
                    anomalies,
                    code="FAILED_RUN_WITH_VALID_FLAG",
                    severity="critical",
                    title="失败任务已出现有效 Flag",
                    summary="任务虽然标记为引擎失败，但已经存在人工验证通过的 Flag 候选，应该把任务状态回收为已解出。",
                    evidence=valid_flags[:3],
                    suggestion="触发 flag/状态重算，确保题目与任务状态同步为已解出。",
                )
            elif open_flags:
                _append_anomaly(
                    anomalies,
                    code="FAILED_ENGINE_NEEDS_REVIEW",
                    severity="medium",
                    title="引擎失败但仍有候选",
                    summary="引擎失败时保留了可复核的 Flag 候选，适合由人工继续裁定而不是直接放弃。",
                    evidence=open_flags[:3],
                    suggestion="先人工确认 Flag 候选，再决定是否需要重新运行或修改方法论。",
                )

        if model and run.engine_type == "openai_compatible":
            _append_anomaly(
                anomalies,
                code="MODEL_CONFIG_USED",
                severity="info",
                title="模型配置参与解题",
                summary=f"本次运行使用模型配置 {model.name}（{model.model_name or 'unknown model'}）。",
                evidence=[model.name],
                suggestion="如果出现重复失败，优先检查模型配置、提示词和工具权限的匹配关系。",
            )

        tags = [item["code"] for item in anomalies]
        if run.last_error_code:
            tags.append(run.last_error_code)
        if no_progress_count:
            tags.append("NO_PROGRESS")
        if state and state.skill_recommendations_json:
            tags.append("SKILL_RECOMMENDATIONS")

        active_skill_names = await self._active_skill_names(session, state)
        diagnostic_summary = anomalies[0]["summary"] if anomalies else run.last_error_message
        return {
            "diagnostic_tags": list(dict.fromkeys(tags)),
            "diagnostic_summary": diagnostic_summary,
            "anomalies": anomalies,
            "state": {
                "current_phase": state.current_phase if state else run.current_phase,
                "no_progress_count": no_progress_count,
                "active_skill_names": active_skill_names,
                "recommended_skills": state.skill_recommendations_json if state else [],
            },
            "challenge": {
                "name": challenge.name if challenge else None,
                "challenge_type": challenge.challenge_type if challenge else None,
                "target_summary": challenge.target_url if challenge and challenge.target_url else (challenge.description[:120] if challenge else None),
            },
            "engine": {
                "engine_type": run.engine_type,
                "model_name": model.name if model else None,
            },
        }

    async def recent(self, session: AsyncSession, limit: int = 25) -> list[dict]:
        runs = list(
            (
                await session.scalars(
                    select(SolveRun).order_by(SolveRun.created_at.desc()).limit(limit)
                )
            ).all()
        )
        return [
            {"run_id": run.id, **await self.analyze(session, run)}
            for run in runs
        ]


run_diagnostics_service = RunDiagnosticsService()
