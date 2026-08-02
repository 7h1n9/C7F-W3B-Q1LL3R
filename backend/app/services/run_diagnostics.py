import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.multi_agent import AgentTask
from app.models.run import (
    Artifact,
    FlagCandidate,
    Observation,
    RunAttempt,
    RunEvent,
    RunExecutionLease,
    RunUserInput,
    SolveRun,
    ToolCall,
)
from app.models.skill import Skill
from app.orchestration.state_machine import RunStatus
from app.services.codex_preflight import codex_preflight_service
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
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def write_artifact(self, session: AsyncSession, run: SolveRun) -> Artifact:
        lock = self._locks.setdefault(run.id, asyncio.Lock())
        async with lock:
            existing = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "diagnostic", Artifact.status == "ACTIVE"))
            if existing:
                return existing
            payload = await self.analyze(session, run)
            root = Path(run.workspace_path).resolve()
            path = root / "diagnostics" / "run-diagnostic.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(raw)
            artifact = Artifact(run_id=run.id, artifact_type="diagnostic", file_path="diagnostics/run-diagnostic.json", mime_type="application/json", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), summary="Run diagnostic artifact", status="ACTIVE")
            session.add(artifact)
            await session.commit()
            await event_service.append(session, run.id, "diagnostic.created", {"artifact_id": artifact.id, "path": artifact.file_path})
            return artifact
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
        is_codex = run.engine_type == "codex_sdk"
        is_openai = run.engine_type == "openai_compatible"
        model_source = "CODEX_BRIDGE" if is_codex else ("OPENAI_COMPATIBLE" if is_openai else None)
        model_config_required = is_openai
        model_config_applicable = is_openai
        bridge_ready = bool(codex_preflight_service.last_result() and codex_preflight_service.last_result().get("ready")) if is_codex else False
        preflight_ready = codex_preflight_service.is_ready(run.id) if is_codex else False
        if is_codex and run.model_config_id:
            _append_anomaly(
                anomalies,
                code="MODEL_CONFIG_NOT_APPLICABLE",
                severity="high",
                title="Codex SDK 不适用 ModelConfig",
                summary="Codex SDK 的模型来源是 Codex Bridge，Run 不应绑定 ModelConfig。",
                evidence=[f"model_config_id={run.model_config_id}"],
                suggestion="清除该 Run 的 ModelConfig 绑定，并通过 Codex Bridge 运行。",
            )
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
                "model_source": model_source,
                "model_config_required": model_config_required,
                "model_config_applicable": model_config_applicable,
                "bridge_ready": bridge_ready,
                "preflight_ready": preflight_ready,
            },
        }

    async def system_self_check(self, session: AsyncSession, run: SolveRun) -> dict:
        """Run the small, read-only acceptance check for one persisted Run.

        This intentionally checks durable lifecycle projections instead of
        starting or resuming anything.  It is therefore safe to call after a
        fresh run, while investigating a WAITING_USER run, or during a batch
        acceptance pass.
        """
        terminal_statuses = {
            "COMPLETED_SOLVED",
            "COMPLETED_UNSOLVED",
            "FAILED_ENGINE",
            "FAILED_TOOL",
            "FAILED_RUNNER",
            "TIMEOUT",
            "POLICY_BLOCKED",
            "CANCELLED",
        }
        is_terminal = str(run.status) in terminal_statuses

        running_attempts = int(await session.scalar(select(func.count()).select_from(RunAttempt).where(
            RunAttempt.run_id == run.id,
            RunAttempt.status == "RUNNING",
        )) or 0)
        active_leases = int(await session.scalar(select(func.count()).select_from(RunExecutionLease).where(
            RunExecutionLease.run_id == run.id,
        )) or 0)
        running_tasks = int(await session.scalar(select(func.count()).select_from(AgentTask).where(
            AgentTask.run_id == run.id,
            AgentTask.status.in_(["RUNNING", "CLAIMED"]),
        )) or 0)
        started_tools = int(await session.scalar(select(func.count()).select_from(ToolCall).where(
            ToolCall.run_id == run.id,
            ToolCall.status.in_(["REQUESTED", "STARTED", "RUNNING"]),
        )) or 0)

        checks: list[dict] = []

        def add_check(name: str, passed: bool, details: dict, reason: str | None = None, *, applicable: bool = True) -> None:
            checks.append({
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "applicable": applicable,
                "details": details,
                "reason": reason if not passed else None,
            })

        add_check(
            "terminal_resources",
            not is_terminal or not any((running_attempts, active_leases, running_tasks, started_tools)),
            {
                "terminal": is_terminal,
                "running_attempts": running_attempts,
                "active_leases": active_leases,
                "running_tasks": running_tasks,
                "started_tools": started_tools,
            },
            "terminal run still owns executable resources" if is_terminal and any((running_attempts, active_leases, running_tasks, started_tools)) else None,
            applicable=is_terminal,
        )

        queued_inputs = int(await session.scalar(select(func.count()).select_from(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "QUEUED",
            RunUserInput.consumed_at.is_(None),
        )) or 0)
        consumed_inputs = int(await session.scalar(select(func.count()).select_from(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "CONSUMED",
        )) or 0)
        input_events = list((await session.scalars(select(RunEvent).where(
            RunEvent.run_id == run.id,
            RunEvent.event_type.in_(["user_input.received", "user_input.consumed"]),
        ))).all())
        has_consumed_event = any(event.event_type == "user_input.consumed" for event in input_events)
        checkpoint = dict(run.recovery_checkpoint_json or {})
        counters = dict(checkpoint.get("supervisor_counters") or {})
        input_waiting_failure = str(run.status) == "WAITING_USER" and queued_inputs == 0 and consumed_inputs > 0 and not has_consumed_event
        input_next_action = (
            "terminal"
            if is_terminal
            else "consume_user_input"
            if queued_inputs
            else "continue_supervisor"
            if str(run.status) != "WAITING_USER"
            else "wait_for_user_input"
        )
        add_check(
            "user_input_recovery",
            not input_waiting_failure,
            {
                "queued": queued_inputs,
                "consumed": consumed_inputs,
                "consumed_event": has_consumed_event,
                "next_action": input_next_action,
                "no_progress_count": int(counters.get("no_progress_count") or 0),
            },
            "consumed input has no durable user_input.consumed event" if input_waiting_failure else None,
        )

        state = await solver_state_service.load(session, run.id)
        ledger = dict(state.capability_ledger_json or {}) if state else {}
        failure_counts = dict(checkpoint.get("tool_failure_counts") or {})
        failure_counts.update(dict(ledger.get("tool_failure_counts") or {}))
        over_budget = [
            {
                "fingerprint": key,
                "tool_name": value.get("tool_name"),
                "stage": value.get("stage"),
                "count": int(value.get("count") or 0),
            }
            for key, value in failure_counts.items()
            if isinstance(value, dict) and int(value.get("count") or 0) > 2
        ]
        add_check(
            "tool_failure_circuit_breaker",
            not over_budget,
            {"tracked_fingerprints": len(failure_counts), "over_budget": over_budget},
            "a tool failure fingerprint exceeded the two-attempt budget" if over_budget else None,
        )

        add_check(
            "phase_consistency",
            state is None or str(state.current_phase or "") == str(run.current_phase or ""),
            {
                "run_phase": run.current_phase,
                "solver_state_phase": state.current_phase if state else None,
            },
            "SolveRun and SolverState current_phase differ" if state and str(state.current_phase or "") != str(run.current_phase or "") else None,
            applicable=state is not None,
        )

        if str(run.status) == "COMPLETED_UNSOLVED":
            report = dict(run.report_json or {})
            required_wp_keys = ("confirmed_facts", "completed_stages", "failed_tools", "user_inputs", "next_steps")
            missing_wp_keys = [key for key in required_wp_keys if key not in report]
            add_check(
                "writeup_completeness",
                not missing_wp_keys,
                {"missing_keys": missing_wp_keys, "keys": sorted(report)},
                "COMPLETED_UNSOLVED report is missing required WP fields" if missing_wp_keys else None,
                applicable=True,
            )
        else:
            add_check("writeup_completeness", True, {"status": str(run.status)}, applicable=False)

        observations = list((await session.scalars(select(Observation).where(Observation.run_id == run.id))).all())
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())
        legacy_contract_hits = [
            "observation" for item in observations
            if "RESULT_CONTRACT" in _blob(item.summary, item.facts_json)
        ] + [
            "event" for item in events
            if "RESULT_CONTRACT" in _blob(item.event_type, item.payload_json)
        ]
        add_check(
            "runner_result_contract",
            not legacy_contract_hits,
            {"legacy_result_contract_hits": len(legacy_contract_hits)},
            "ordinary metadata empty-result was recorded as legacy RESULT_CONTRACT" if legacy_contract_hits else None,
        )

        failures = [check for check in checks if check["status"] == "FAIL"]
        return {
            "run_id": run.id,
            "status": "FAIL" if failures else "PASS",
            "run_status": str(run.status),
            "checks": checks,
            "reasons": [check["reason"] for check in failures if check.get("reason")],
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
