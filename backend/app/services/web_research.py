"""Bounded Web Research with query classification and answer-leak guards."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import SolveRun, WebResearchRecord
from app.services.temporary_data import temporary_workspace


class SearchAdapter(Protocol):
    async def search(self, query: str) -> dict[str, Any]: ...


class CodexWebSearchAdapter:
    def __init__(self, url: str, api_key: str = "", timeout: int = 20) -> None:
        self.url, self.api_key, self.timeout = url, api_key, timeout

    async def search(self, query: str) -> dict[str, Any]:
        if not self.url:
            raise DomainError("WEB_SEARCH_NOT_CONFIGURED", "Codex Web Search endpoint is not configured.", status_code=503)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            response = await client.post(self.url, json={"query": query}, headers=headers)
            response.raise_for_status()
            body = response.json()
        return body if isinstance(body, dict) else {"results": []}


class OpenAICompatibleWebSearchAdapter(CodexWebSearchAdapter):
    async def search(self, query: str) -> dict[str, Any]:
        if not self.url:
            raise DomainError("WEB_SEARCH_NOT_CONFIGURED", "OpenAI-compatible Web Search endpoint is not configured.", status_code=503)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": "web-search", "messages": [{"role": "user", "content": query}], "temperature": 0}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            response = await client.post(self.url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        choices = payload.get("choices") or []
        content = choices[0].get("message", {}).get("content", "") if choices and isinstance(choices[0], dict) else ""
        return {"summary": str(content), "results": payload.get("sources") or []}


@dataclass(frozen=True)
class QueryRisk:
    risk_level: str
    answer_leak_risk: str
    reason: str


class QueryRiskClassifier:
    BLOCKED_TERMS = ("flag", "answer", "solution", "writeup", "secret", "password", "source code", "challenge file", "historical run")

    def classify(self, query: str, challenge: Challenge | None = None, known_answers: list[str] | None = None) -> QueryRisk:
        normalized = " ".join(query.lower().split())
        if re.search(r"(?i)(?:[a-z]:[\\/]|/home/|/workspace/|\\\\)", query):
            return QueryRisk("BLOCKED", "HIGH", "local path or challenge source access")
        challenge_name = str(getattr(challenge, "name", "") or "").strip().lower()
        challenge_target = str(getattr(challenge, "target_url", "") or "").strip().lower()
        if challenge_name and challenge_name in normalized:
            return QueryRisk("BLOCKED", "HIGH", "challenge name disclosure")
        if challenge_target and challenge_target in normalized:
            return QueryRisk("BLOCKED", "HIGH", "target disclosure")
        if any(answer and answer.lower() in normalized for answer in (known_answers or [])):
            return QueryRisk("BLOCKED", "HIGH", "known answer disclosure")
        if "flag{" in normalized or any(term in normalized for term in self.BLOCKED_TERMS):
            return QueryRisk("HIGH", "HIGH", "answer-oriented research request")
        if any(term in normalized for term in ("exact payload", "dump the table", "extract the value", "specific endpoint")):
            return QueryRisk("MEDIUM", "MEDIUM", "target-specific extraction request")
        return QueryRisk("LOW", "LOW", "general technique research")


class AnswerLeakGuard:
    def inspect(self, text: str, challenge: Challenge | None = None) -> tuple[bool, str]:
        value = str(text or "")
        challenge_name = str(getattr(challenge, "name", "") or "").strip()
        if challenge_name and challenge_name.lower() in value.lower():
            return False, "<challenge-specific content removed>"
        pattern = str(getattr(challenge, "flag_pattern", "") or "")
        if pattern:
            try:
                if re.search(pattern, value):
                    return False, "<flag-like answer removed>"
            except re.error:
                pass
        if re.search(r"(?i)flag\{[^{}\r\n]{1,256}\}|(?:[a-z]:[\\/]|/home/|/workspace/)", value):
            return False, "<answer or local source removed>"
        return True, value[:4000]


class WebResearchService:
    def __init__(self, adapter: SearchAdapter | None = None) -> None:
        self.adapter = adapter
        self.classifier = QueryRiskClassifier()
        self.leak_guard = AnswerLeakGuard()

    def _adapter(self) -> SearchAdapter:
        if self.adapter is not None:
            return self.adapter
        settings = get_settings()
        if settings.web_search_provider == "codex":
            return CodexWebSearchAdapter(settings.web_search_url, settings.web_search_api_key, settings.web_search_timeout_seconds)
        if settings.web_search_provider in {"openai_compatible", "openai-compatible"}:
            return OpenAICompatibleWebSearchAdapter(settings.web_search_url, settings.web_search_api_key, settings.web_search_timeout_seconds)
        raise DomainError("WEB_SEARCH_NOT_CONFIGURED", "Web Search provider is disabled.", status_code=503)

    @staticmethod
    def _urls(payload: dict[str, Any]) -> list[str]:
        raw = payload.get("results") or payload.get("sources") or []
        urls: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            url = item.get("url") if isinstance(item, dict) else item if isinstance(item, str) else None
            if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in urls:
                urls.append(url[:1000])
        return urls[:20]

    async def search(self, session: AsyncSession, run: SolveRun, task: AgentTask | None, query: str, *, requested_by: str = "ANALYSIS", challenge: Challenge | None = None) -> dict:
        if requested_by not in {"PLANNER", "ANALYSIS"}:
            raise DomainError("WEB_SEARCH_ROLE_FORBIDDEN", "Only Planner and Analysis may request Web Research.")
        risk = self.classifier.classify(query, challenge)
        runtime = temporary_workspace.web_path(Path(run.workspace_path), task.id if task else f"run-{run.id}")
        record = WebResearchRecord(run_id=run.id, agent_task_id=task.id if task else None, query=query[:1000] if risk.risk_level != "BLOCKED" else "<blocked query>", query_type="GENERAL_TECHNIQUE", requested_by=requested_by, risk_level=risk.risk_level, answer_leak_risk=risk.answer_leak_risk, status="BLOCKED" if risk.risk_level == "BLOCKED" else "EPHEMERAL", expires_at=datetime.now(UTC) + timedelta(minutes=60), runtime_path=str(runtime.relative_to(Path(run.workspace_path).resolve())).replace("\\", "/"))
        session.add(record)
        await session.flush()
        if risk.risk_level == "BLOCKED":
            return {"record_id": record.id, "status": "BLOCKED", "risk_level": risk.risk_level, "answer_leak_risk": risk.answer_leak_risk, "summary": "Research query blocked by answer-leak policy.", "source_urls": [], "reason": risk.reason}
        (runtime / "query.json").write_text(json.dumps({"query": query, "risk": risk.__dict__}, ensure_ascii=False), encoding="utf-8")
        try:
            payload = await self._adapter().search(query)
        except DomainError:
            record.status = "WAITING_CONFIGURATION"
            await session.flush()
            raise
        except Exception as error:
            record.status = "FAILED"
            await session.flush()
            raise DomainError("WEB_SEARCH_FAILED", "Web Research provider failed.", {"error": str(error)[:300]}, status_code=502) from error
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        (runtime / "raw-response.json").write_text(raw, encoding="utf-8")
        summary = str(payload.get("summary") or payload.get("answer") or "")
        safe, sanitized = self.leak_guard.inspect(summary, challenge)
        if not safe or risk.answer_leak_risk == "HIGH":
            record.status = "BLOCKED"
            record.answer_leak_risk = "HIGH"
            record.summary = "<answer-like content withheld>"
            record.source_urls_json = []
            await session.flush()
            return {"record_id": record.id, "status": "BLOCKED", "risk_level": risk.risk_level, "answer_leak_risk": "HIGH", "summary": record.summary, "source_urls": [], "reason": "answer leak guard"}
        record.summary = sanitized
        record.source_urls_json = self._urls(payload)
        await session.flush()
        return {"record_id": record.id, "status": record.status, "risk_level": risk.risk_level, "answer_leak_risk": record.answer_leak_risk, "summary": sanitized, "source_urls": record.source_urls_json}

    async def promote(self, session: AsyncSession, record_id: str, fact_ids: list[str], *, role: str = "ANALYSIS") -> WebResearchRecord:
        if role != "ANALYSIS":
            raise DomainError("WEB_RESEARCH_PROMOTION_FORBIDDEN", "Only Analysis may promote Web Research summaries.")
        record = await session.get(WebResearchRecord, record_id)
        if record is None:
            raise DomainError("WEB_RESEARCH_NOT_FOUND", "Web Research record does not exist.", status_code=404)
        if record.status == "BLOCKED":
            raise DomainError("WEB_RESEARCH_BLOCKED", "Blocked Web Research cannot be promoted.")
        record.status = "PROMOTED"
        record.used_in_fact_ids_json = list(dict.fromkeys(fact_ids))
        record.promoted_at = datetime.now(UTC)
        await session.flush()
        return record


web_research_service = WebResearchService()
