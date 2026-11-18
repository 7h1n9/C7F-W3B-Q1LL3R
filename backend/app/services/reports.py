import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.multi_agent import EvidenceLedger
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
from app.services.manual_writeup import ManualWriteupRenderer
from app.services.muteki_board_semantics import SEMANTIC_CACHE_KEY
from app.services.muteki_llm_report import LLM_REPORT_CACHE_KEY, build_llm_report
from app.services.muteki_poc_bundle import PocBundleUnavailable, build_poc_bundle_for_run
from app.services.muteki_recovered_trace import load_recovered_trace, merge_recovered_state
from app.services.muteki_writeup import render_muteki_writeup
from app.services.reproduction_commands import reproduction_command_renderer
from app.solver.muteki.adapter.cost_bridge import load_muteki_usage

logger = logging.getLogger(__name__)


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
        # A Muteki worker's own record of "a worker ran".  It carries no request:
        # replaying it produced ``curl -i -X GET "{{target_url}}"`` -- a literal
        # template with an empty URL, which "reproduces" nothing.  Measured
        # 2026-11-10 on run 9847adf6, where this was the only ToolCall row and it
        # shadowed the real evidence (the worker's own PoC script on disk).
        "muteki_native_worker",
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
            if (
                call.status != "COMPLETED"
                or call.tool_name in self._EXCLUDED_TOOLS
                or call.tool_name.startswith("ctfctl.")
            ):
                continue
            raw_arguments = call.arguments_json or {}
            if call.tool_name in {"http_request", "http_session_request"}:
                # Do not turn an incomplete legacy trace into a literal
                # {{target_url}} GET reproduction when a later complete
                # authorized RequestSpec exists in the same run.
                if not raw_arguments.get("url"):
                    continue
                if str(raw_arguments.get("method") or "GET").upper() == "GET" and not (
                    raw_arguments.get("query") or raw_arguments.get("json") or raw_arguments.get("form") or raw_arguments.get("body")
                ):
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
            normalized = self._normalize(raw_arguments)
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
        if not steps:
            steps = self._workspace_poc_steps(run)
        return steps

    #: PoC scripts a Muteki worker leaves behind, newest-looking first.  The worker
    #: writes ``exploit.py`` / ``exploit2.py`` / ``PoC_*.py`` into its own
    #: ``muteki/`` scratch dir and sometimes copies the winner to ``shared/``.
    _POC_GLOBS: tuple[str, ...] = (
        "muteki/exploit*.py",
        "shared/PoC_*.py",
        "muteki/PoC_*.py",
        "muteki/solve*.py",
    )

    @classmethod
    def _workspace_poc_steps(cls, run: SolveRun) -> list[ReproductionStep]:
        """Fall back to the PoC script the Worker wrote when no ToolCall replays.

        Measured 2026-11-10 on ``[De1CTF 2019]Giftbox`` (run ``9847adf6``): the Run
        has exactly one ``ToolCall`` -- ``muteki_native_worker`` -- because Muteki
        workers run shell commands directly instead of going through the structured
        ``http_request`` tool.  ``extract`` therefore returned **0 steps** and
        ``execute`` blocked with ``MISSING_EXTRACTION_PROVENANCE`` before running
        anything.  The replay was impossible not because the evidence was missing
        but because it was in the wrong shape.

        The evidence was on disk the whole time: the worker had written
        ``muteki/exploit2.py`` and copied it to ``shared/PoC_giftbox_exploit.py``
        after its own end-to-end verification, and ``final/muteki-poc.zip`` is the
        packaged form of the same thing.

        This does NOT weaken the check.  Re-running the Worker's own script in a
        fresh container is a stronger reproduction than replaying a hand-assembled
        curl: it re-executes the actual exploit chain rather than one request from
        it.  The flag is still matched against ``challenge.flag_pattern`` by the
        caller, so a script that prints nothing is still unverified.
        """

        root = Path(str(getattr(run, "workspace_path", "") or ""))
        if not root or not root.is_dir():
            return []
        chosen: Path | None = None
        for pattern in cls._POC_GLOBS:
            matches = sorted(
                (p for p in root.glob(pattern) if p.is_file() and p.stat().st_size > 0),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                chosen = matches[0]
                break
        if chosen is None:
            return []
        relative = chosen.relative_to(root).as_posix()
        interpreter = "python3" if chosen.suffix == ".py" else "bash"
        command = f"cd {{workspace}} && {interpreter} {relative}"
        return [
            ReproductionStep(
                order=1,
                title_zh="重放 Worker 留下的 PoC 脚本",
                purpose_zh=(
                    "在全新容器中重新执行 Worker 自己写出的利用脚本，"
                    "验证整条利用链可独立复现。"
                ),
                tool_name="workspace_poc",
                normalized_arguments={"path": relative, "interpreter": interpreter},
                manual_method=f"{interpreter} {relative}",
                manual_command=command,
                expected_evidence=[f"脚本 {relative} 的输出中出现 flag"],
                source_artifact_ids=[],
            )
        ]


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
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def generate(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        result: str,
        failure_reason: str = "",
    ) -> Artifact:
        lock = self._locks.setdefault(run.id, asyncio.Lock())
        async with lock:
            existing = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))
            if existing:
                return existing
            return await self._generate(session, run, challenge, result, failure_reason)

    async def generate_muteki(
        self,
        session: AsyncSession,
        run: SolveRun,
        result: Any,
    ) -> Artifact:
        """Materialize the canonical Muteki result as a formal report.

        Muteki owns completion evidence in its append-only graph rather than
        the legacy ``FlagCandidate`` review path.  Its terminal result still
        needs the same report artifacts so the existing Run API and frontend
        can consume a completed run consistently.  This adapter intentionally
        stores only sanitized outcome metadata and Evidence references.
        """

        existing = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run.id,
                Artifact.artifact_type == "report",
                Artifact.status == "ACTIVE",
            )
        )
        if existing is not None:
            return existing

        challenge = await session.get(Challenge, run.challenge_id)
        if challenge is None:
            raise ValueError("MUTEKI_CHALLENGE_NOT_FOUND")
        evidence_refs = [
            str(item)
            for item in await session.scalars(
                select(EvidenceLedger.id)
                .where(EvidenceLedger.run_id == run.id)
                .order_by(EvidenceLedger.created_at)
            )
        ]
        status = str(getattr(result, "status", run.status))
        solved = status == RunStatus.COMPLETED_SOLVED.value
        graph_path = Path(str(getattr(result, "graph_path", "") or "")).resolve()
        if not graph_path.is_file():
            graph_path = Path(run.workspace_path).resolve() / "muteki" / "graph" / "upstream_shared_graph.db"
        from app.solver.muteki.adapter.graph_snapshot import read_native_graph_snapshot

        graph_state = await asyncio.to_thread(
            read_native_graph_snapshot,
            db_path=graph_path,
            challenge=challenge,
            run_id=str(run.id),
        )
        recovered_trace = await asyncio.to_thread(
            load_recovered_trace,
            workspace=str(run.workspace_path),
            target_url=str(getattr(challenge, "target_url", "") or ""),
        )
        graph_state = merge_recovered_state(graph_state, recovered_trace)
        semantic_analysis = dict((run.hints_json or {}).get(SEMANTIC_CACHE_KEY) or {})
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
                    select(Observation).where(Observation.run_id == run.id).order_by(Observation.created_at)
                )
            ).all()
        )
        evidence_rows = list(
            (
                await session.scalars(
                    select(EvidenceLedger).where(EvidenceLedger.run_id == run.id).order_by(EvidenceLedger.created_at)
                )
            ).all()
        )
        recovered_evidence_refs = [
            str(item.get("id"))
            for item in recovered_trace.get("evidence", [])
            if item.get("id") and str(item.get("status") or "").upper() == "VERIFIED"
        ]
        evidence_refs = list(dict.fromkeys([*evidence_refs, *recovered_evidence_refs]))
        poc_bundle = None
        try:
            poc_bundle = await build_poc_bundle_for_run(session, run, challenge, graph_state)
        except PocBundleUnavailable:
            # A solved graph can be valid while not containing a replayable
            # HTTP request.  Keep the report truthful and expose the missing
            # condition in the WP instead of manufacturing a script.
            poc_bundle = None
        usage = await load_muteki_usage(session, str(run.id))
        # Narrative authoring: hand the model the Worker's captured conversation
        # plus the deterministic board projection, and let it write the sections
        # the template renderer structurally cannot.  Never fatal — a failure
        # degrades to the deterministic writeup.
        llm_report = await build_llm_report(
            session,
            run=run,
            challenge=challenge,
            graph_state=graph_state,
            calls=calls,
            evidence=evidence_rows,
            poc_available=poc_bundle is not None,
            verified_flag=str(getattr(result, "flag", "") or "") if solved else "",
            usage_summary={
                "calls": usage.calls,
                "cost_usd": round(usage.cost_usd, 10),
                "total_tokens": usage.tokens,
            },
        )
        report_payload = {
            "result": "solved" if solved else "unsolved",
            "engine": "muteki",
            "status": status,
            "reason": str(getattr(result, "reason", "")),
            "flag": getattr(result, "flag", None) if solved else None,
            "flag_verified": bool(getattr(result, "flag_found", False)) if solved else False,
            "evidence_refs": evidence_refs,
            "evidence_count": len(evidence_refs),
            "evidence_sources": (
                (["evidence_ledger"] if evidence_rows else [])
                + (["recovered_execution_record"] if recovered_evidence_refs else [])
            ),
            "graph_path": str(graph_path),
            "completed_stages": ["prepare", "race", "coordinator", "finalize"],
            "tool_call_count": int(run.tool_call_count or 0),
            "graph_revision": int(graph_state.get("revision") or 0),
            "poc_reproducible": poc_bundle is not None,
            "poc_bundle": {
                "path": "final/muteki-poc.zip",
                "step_count": len(poc_bundle.request_plan),
                "evidence_refs": list(poc_bundle.evidence_refs),
            } if poc_bundle is not None else None,
            "recovered_trace": {
                "available": bool(recovered_trace.get("available")),
                "step_count": len(recovered_trace.get("steps", []) or []),
                "replayable_step_count": len(recovered_trace.get("replayable_steps", []) or []),
            },
            "llm_report": {
                "status": llm_report.get("status"),
                "model": llm_report.get("model") or "",
                "conversation_available": bool(llm_report.get("conversation_available")),
                "conversation_chars": int(llm_report.get("conversation_chars") or 0),
                "error_code": llm_report.get("error_code") or "",
                "usage": llm_report.get("usage") or {},
            },
            "semantic_analysis": {
                "status": semantic_analysis.get("status"),
                "model": semantic_analysis.get("model"),
                "revision": semantic_analysis.get("revision"),
                "item_count": len(semantic_analysis.get("items", []) or []),
                "usage": semantic_analysis.get("usage"),
            },
        }
        if usage.calls:
            report_payload.update(
                {
                    "cost_usd": round(usage.cost_usd, 10),
                    "total_tokens": usage.tokens,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "llm_calls": usage.calls,
                }
            )
        run.report_json = report_payload

        root = Path(run.workspace_path).resolve()
        final = root / "final"
        final.mkdir(parents=True, exist_ok=True)
        if poc_bundle is not None:
            (final / "muteki-poc.zip").write_bytes(poc_bundle.content)
        report_json_raw = json.dumps(report_payload, ensure_ascii=False, indent=2).encode("utf-8")
        (final / "report.json").write_bytes(report_json_raw)

        writeup = render_muteki_writeup(
            challenge=challenge,
            run=run,
            result=result,
            graph_state=graph_state,
            calls=calls,
            observations=observations,
            evidence=evidence_rows,
            poc_available=poc_bundle is not None,
            recovered_trace=recovered_trace,
            semantic_analysis=semantic_analysis,
            llm_report=llm_report,
        ).encode("utf-8")
        (final / "writeup.zh-CN.md").write_bytes(writeup)
        (final / "writeup.md").write_bytes(writeup)

        report_artifact = Artifact(
            run_id=run.id,
            artifact_type="report",
            file_path="final/writeup.zh-CN.md",
            mime_type="text/markdown",
            size=len(writeup),
            sha256=hashlib.sha256(writeup).hexdigest(),
            summary="Muteki canonical graph completion report",
            status="ACTIVE",
        )
        report_json_artifact = Artifact(
            run_id=run.id,
            artifact_type="report_json",
            file_path="final/report.json",
            mime_type="application/json",
            size=len(report_json_raw),
            sha256=hashlib.sha256(report_json_raw).hexdigest(),
            summary="Sanitized Muteki completion payload",
            status="ACTIVE",
        )
        poc_artifact = None
        if poc_bundle is not None:
            poc_artifact = Artifact(
                run_id=run.id,
                artifact_type="poc_bundle",
                file_path="final/muteki-poc.zip",
                mime_type="application/zip",
                size=len(poc_bundle.content),
                sha256=hashlib.sha256(poc_bundle.content).hexdigest(),
                summary="Evidence-backed reusable Muteki PoC bundle",
                status="ACTIVE",
            )
        session.add(report_artifact)
        session.add(report_json_artifact)
        if poc_artifact is not None:
            session.add(poc_artifact)
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "report.completed",
            {"artifact_id": report_artifact.id, "engine": "muteki", "evidence_count": len(evidence_refs)},
        )
        return report_artifact

    async def _generate(
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
        from app.services.run_finalizer import run_finalizer

        wp_payload = await run_finalizer.build_wp(session, run, failure_reason or result)
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
        minimal_path = {
            "minimal_solution_path": [item.model_dump() for item in steps],
            "confirmation_path": [item.model_dump() for item in steps if item.tool_name in {"http_request", "http_session_request", "sql_boolean_compare", "sql_injection_probe"}],
            "automation_path": [item.model_dump() for item in steps if item.tool_name in {"sqlmap_detect", "sqlmap_run", "script_run", "python_run"}],
            "verification_path": [item.model_dump() for item in steps if item.tool_name in {"http_request", "http_session_request"}][-1:],
        }
        (final / "minimal-solution-path.json").write_text(json.dumps(minimal_path, ensure_ascii=False, indent=2), encoding="utf-8")
        (final / "reproduction-commands.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n" + reproduction_command_renderer.render_steps([item.model_dump() for item in steps]) + "\n",
            encoding="utf-8",
        )
        for directory in ("requests", "scripts", "sqlmap"):
            source = root / directory
            destination = final / directory
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
        (final / "evidence-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report_json = {"result": result, "failure_reason": failure_reason, "wp": wp_payload}
        run.report_json = wp_payload
        report_json_raw = json.dumps(report_json, ensure_ascii=False, indent=2).encode("utf-8")
        (final / "report.json").write_bytes(report_json_raw)
        raw = ManualWriteupRenderer().render(
            challenge, run, result, calls, observations, hypotheses, flags, steps, failure_reason, wp_payload
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
        wp_artifact = Artifact(
            run_id=run.id,
            artifact_type="report_json",
            file_path="final/report.json",
            mime_type="application/json",
            size=len(report_json_raw),
            sha256=hashlib.sha256(report_json_raw).hexdigest(),
            summary="Durable report payload including WP facts, inputs and failure history.",
            status="ACTIVE",
        )
        old_reports = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))).all())
        for old in old_reports:
            old.status = "STALE"
        old_wp_reports = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report_json", Artifact.status == "ACTIVE"))).all())
        for old in old_wp_reports:
            old.status = "STALE"
        session.add(artifact)
        session.add(wp_artifact)
        await session.commit()
        await event_service.append(
            session, run.id, "artifact.created", {"artifact_id": artifact.id, "path": artifact.file_path}
        )
        await event_service.append(
            session, run.id, "report.completed", {"artifact_id": artifact.id, "reproduction": verifier}
        )
        return artifact


report_service = ReportService()


def _flag_shaped(value: Any) -> str | None:
    """Return a real flag token, or None for empty/placeholder/non-flag values.

    Recovery must never turn a template the model echoed (``flag{...}``,
    ``{uuid}``, ``flag{your_flag_here}``) into a persisted answer, so the
    upstream provenance gate's placeholder rule is reused here.
    """

    text = str(value or "").strip()
    if not text or len(text) > 512 or "\n" in text or "\r" in text:
        return None
    if "{" not in text or not text.endswith("}"):
        return None
    try:
        from muteki.solver.gate import is_placeholder_flag
    except Exception:  # pragma: no cover - import guard for isolated runtimes
        return text
    return None if is_placeholder_flag(text) else text


async def recover_muteki_terminal_result(session: AsyncSession, run: SolveRun) -> SimpleNamespace:
    """Rebuild the canonical Muteki completion result from durable evidence.

    A Muteki Run can reach COMPLETED_SOLVED / COMPLETED_UNSOLVED through the
    native terminal handoff (``run.muteki_terminal_reconciled``) or through
    startup recovery instead of the in-process runtime.  No in-memory
    ``MutekiRunResult`` survives that path, so report materialization had no
    result object to consume and the final report, writeup and PoC bundle were
    never written — leaving a "completed" Run that could neither show nor export
    its Writeup or PoC.

    This reconstructs the same shape from the Run row, the durable terminal
    event and the native graph, without ever inventing a flag.
    """

    report = run.report_json if isinstance(run.report_json, dict) else {}
    checkpoint = run.recovery_checkpoint_json if isinstance(run.recovery_checkpoint_json, dict) else {}
    terminal = checkpoint.get("muteki_native_terminal") if isinstance(checkpoint.get("muteki_native_terminal"), dict) else {}
    status = str(run.status)

    flag = _flag_shaped(report.get("flag"))
    flag_found = bool(report.get("flag_verified")) or bool(terminal.get("flag_found")) or status == RunStatus.COMPLETED_SOLVED.value

    graph_path = str(report.get("graph_path") or "").strip()
    if not graph_path:
        graph_path = str(Path(str(run.workspace_path)).resolve() / "muteki" / "graph" / "upstream_shared_graph.db")

    challenge = await session.get(Challenge, run.challenge_id)
    if flag is None and challenge is not None and Path(graph_path).is_file():
        try:
            from app.solver.muteki.adapter.graph_snapshot import read_native_graph_snapshot

            state = await asyncio.to_thread(
                read_native_graph_snapshot,
                db_path=Path(graph_path),
                challenge=challenge,
                run_id=str(run.id),
            )
            for item in reversed([entry for entry in (state.get("flags") or []) if entry.get("verified")]):
                flag = _flag_shaped(item.get("flag"))
                if flag:
                    break
        except Exception:
            logger.warning("muteki terminal flag recovery from graph failed", exc_info=True)

    if flag is None:
        candidate = await session.scalar(
            select(FlagCandidate)
            .where(FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(True))
            .order_by(FlagCandidate.created_at.desc())
        )
        if candidate is not None:
            flag = _flag_shaped(getattr(candidate, "candidate", None))

    if flag is not None:
        flag_found = True

    reason = str(
        terminal.get("reason")
        or report.get("reason")
        or run.last_error_code
        or "MUTEKI_TERMINAL_RECONCILED"
    )
    return SimpleNamespace(
        status=status,
        reason=reason,
        flag=flag,
        flag_found=bool(flag_found),
        graph_path=graph_path,
    )


async def ensure_muteki_report(session: AsyncSession, run: SolveRun) -> bool:
    """Idempotently materialize the canonical Muteki report for export.

    Returns True when an ACTIVE formal report now exists.  Safe to call on a
    read path: when the report was already generated this is a single indexed
    lookup that changes nothing.
    """

    if str(run.solver_mode or "").lower() != "muteki":
        return False
    if str(run.status) not in {
        RunStatus.COMPLETED_SOLVED.value,
        RunStatus.COMPLETED_UNSOLVED.value,
    }:
        return False

    existing = await session.scalar(
        select(Artifact.id).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "report",
            Artifact.status == "ACTIVE",
        )
    )
    if existing is not None:
        return True

    try:
        result = await recover_muteki_terminal_result(session, run)
        await report_service.generate_muteki(session, run, result)
    except Exception:
        logger.exception("Muteki report materialization failed for run %s", run.id)
        await session.rollback()
        return False

    created = await session.scalar(
        select(Artifact.id).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "report",
            Artifact.status == "ACTIVE",
        )
    )
    if created is not None:
        # The reconciliation path never emitted this, so record it explicitly
        # for the Run timeline.
        await event_service.append(
            session,
            run.id,
            "report.materialized",
            {"artifact_id": str(created), "engine": "muteki", "source": "terminal_reconciliation"},
        )
        await session.commit()
    return created is not None
