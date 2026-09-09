"""Presentation-only Codex summaries for the Muteki investigation board.

This service is deliberately outside the Solver/Worker path.  It receives a
sanitized projection of already verified Blackboard facts, asks the Codex
Bridge for a text-only JSON response, and stores the result in ``hints_json``
as UI cache.  It never writes SharedGraph, Evidence or Solver control state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.run import SolveRun

SEMANTIC_CACHE_KEY = "muteki_board_semantic"
_locks: dict[str, asyncio.Lock] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def _lock_for(run_id: str) -> asyncio.Lock:
    return _locks.setdefault(str(run_id), asyncio.Lock())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _redact(value: str) -> str:
    """Remove secrets and flag values before sending board text to Codex."""

    result = str(value or "")
    result = re.sub(r"flag\{[^}\r\n]*\}", "flag{[REDACTED]}", result, flags=re.IGNORECASE)
    result = re.sub(
        r"(password|passwd|token|secret|api[_ -]?key|authorization|cookie)\s*[:=]\s*([^,;\s}`]+)",
        r"\1=[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"(demo credentials?|credentials?)\s+([\w.-]+)\s*/\s*([\w.-]+)",
        r"\1 [REDACTED] / [REDACTED]",
        result,
        flags=re.IGNORECASE,
    )
    return result[:700]


def build_board_inputs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the bounded, non-sensitive model input from a graph projection."""

    rows: list[dict[str, Any]] = []
    for item in snapshot.get("key_conditions") or []:
        if not isinstance(item, dict):
            continue
        sequence = item.get("sequence")
        if not isinstance(sequence, int):
            continue
        rows.append(
            {
                "card_id": f"fact-{sequence}",
                "sequence": sequence,
                "local_summary": _redact(str(item.get("summary_zh") or item.get("content") or "")),
                "evidence_count": len(item.get("evidence_refs") or []),
            }
        )
    return rows[:32]


def board_fingerprint(revision: int, items: list[dict[str, Any]]) -> str:
    payload = json.dumps({"revision": revision, "items": items}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt(items: list[dict[str, Any]]) -> str:
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return (
        "你是 Web 安全案情分析板的只读语义整理器。\n"
        "只处理下面已经确认且有证据引用的事实，不推测新的漏洞，不补充原文中没有的信息。\n"
        "为每张卡片生成简短、清晰的中文摘要，保留 HTTP 方法、路径和状态码等技术锚点。\n"
        "每条 summary_zh 不超过 90 个汉字；category 使用中文；importance 只能是 high、medium、low。\n"
        "必须返回 JSON：{\"items\":[{\"card_id\":\"fact-1\",\"summary_zh\":\"...\",\"category\":\"...\",\"importance\":\"high\"}]}。\n"
        "不得输出 Markdown、解释、密码、Cookie、Token、Secret 或 Flag 原文。\n"
        f"待整理卡片：{payload}"
    )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["card_id", "summary_zh", "category", "importance"],
                    "properties": {
                        "card_id": {"type": "string"},
                        "summary_zh": {"type": "string", "maxLength": 180},
                        "category": {"type": "string", "maxLength": 30},
                        "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                },
            }
        },
    }


def parse_bridge_events(events: list[dict[str, Any]], expected_ids: set[str]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Extract and validate the final JSON message from Bridge events."""

    messages = [
        str(event.get("payload", {}).get("message") or "")
        for event in events
        if event.get("type") == "agent.message"
    ]
    raw = next((message for message in reversed(messages) if message.strip()), "")
    if not raw:
        raise ValueError("BOARD_SEMANTIC_EMPTY_RESPONSE")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("BOARD_SEMANTIC_INVALID_JSON") from error
    rows = document.get("items") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("BOARD_SEMANTIC_ITEMS_MISSING")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        card_id = str(row.get("card_id") or "")
        summary = _redact(str(row.get("summary_zh") or "")).strip()
        category = str(row.get("category") or "事实").strip()[:30]
        importance = str(row.get("importance") or "medium").lower()
        if card_id not in expected_ids or card_id in seen or not summary:
            continue
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        result.append({"card_id": card_id, "summary_zh": summary[:180], "category": category, "importance": importance})
        seen.add(card_id)
    if not result:
        raise ValueError("BOARD_SEMANTIC_NO_VALID_ITEMS")
    usage = next(
        (event.get("payload", {}).get("usage") for event in reversed(events) if event.get("type") == "agent.turn_completed"),
        {},
    )
    usage = usage if isinstance(usage, dict) else {}
    return result, {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)),
    }


async def _call_llm(run_id: str, items: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Use the Run's Reason model (or the first enabled openai_compatible config)
    for board semantic analysis."""
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.models.model_config import ModelConfig
    from app.models.run import SolveRun
    from app.services.crypto import decrypt_api_key
    from app.solver.muteki.runtime.configuration import runtime_selection_from_hints

    async with SessionLocal() as session:
        run = await session.get(SolveRun, run_id)
        if not run:
            raise ValueError("BOARD_SEMANTIC_RUN_NOT_FOUND")
        reason_id, _ = runtime_selection_from_hints(run.hints_json)
        config = None
        if reason_id:
            config = await session.get(ModelConfig, reason_id)
        if not config or not config.enabled or config.provider_type != "openai_compatible":
            # Fallback to first enabled openai_compatible config
            config = (await session.scalars(
                select(ModelConfig).where(ModelConfig.enabled, ModelConfig.provider_type == "openai_compatible").limit(1)
            )).first()
        if not config or not config.encrypted_api_key or not config.base_url:
            raise ValueError("BOARD_SEMANTIC_MODEL_UNAVAILABLE")
        api_key = decrypt_api_key(config.encrypted_api_key)
        model = str(config.model_name or config.name)
        base_url = str(config.base_url).rstrip("/")
    timeout = httpx.Timeout(connect=10, read=180, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _prompt(items)}],
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
    return _parse_llm_message(message, {str(item["card_id"]) for item in items}), {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _parse_llm_message(raw: str, expected_ids: set[str]) -> list[dict[str, str]]:
    """Extract and validate the final JSON message from LLM response."""
    import json
    if not raw:
        raise ValueError("BOARD_SEMANTIC_EMPTY_RESPONSE")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("BOARD_SEMANTIC_INVALID_JSON") from error
    rows = document.get("items") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("BOARD_SEMANTIC_ITEMS_MISSING")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        card_id = str(row.get("card_id") or "")
        summary = _redact(str(row.get("summary_zh") or "")).strip()
        category = str(row.get("category") or "事实").strip()[:30]
        importance = str(row.get("importance") or "medium").lower()
        if card_id not in expected_ids or card_id in seen or not summary:
            continue
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        result.append({"card_id": card_id, "summary_zh": summary[:180], "category": category, "importance": importance})
        seen.add(card_id)
    if not result:
        raise ValueError("BOARD_SEMANTIC_NO_VALID_ITEMS")
    return result


async def _save_cache(run_id: str, cache: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        run = await session.get(SolveRun, run_id)
        if run is None:
            return
        hints = dict(run.hints_json or {})
        hints[SEMANTIC_CACHE_KEY] = cache
        run.hints_json = hints
        await session.commit()


async def _run_background(run_id: str, fingerprint: str, revision: int, items: list[dict[str, Any]]) -> None:
    try:
        summaries, usage = await asyncio.wait_for(_call_llm(run_id, items), timeout=180)
        await _save_cache(
            run_id,
            {
                "status": "completed",
                "model": "openai-compatible",
                "revision": revision,
                "fingerprint": fingerprint,
                "items": summaries,
                "usage": usage,
                "updated_at": _now(),
            },
        )
    except TimeoutError:
        error_code = "BOARD_SEMANTIC_TIMEOUT"
        await _save_cache(
            run_id,
            {
                "status": "failed",
                "model": "openai-compatible",
                "revision": revision,
                "fingerprint": fingerprint,
                "items": [],
                "error_code": error_code,
                "updated_at": _now(),
            },
        )
    except Exception as error:
        error_code = str(error).strip() or type(error).__name__.upper()
        await _save_cache(
            run_id,
            {
                "status": "failed",
                "model": "openai-compatible",
                "revision": revision,
                "fingerprint": fingerprint,
                "items": [],
                "error_code": error_code[:120],
                "updated_at": _now(),
            },
        )
    finally:
        _tasks.pop(str(run_id), None)


async def request_analysis(
    session: AsyncSession,
    *,
    run: SolveRun,
    revision: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Start or reuse a semantic analysis without touching Solver state."""

    fingerprint = board_fingerprint(revision, items)
    existing = dict((run.hints_json or {}).get(SEMANTIC_CACHE_KEY) or {})
    if existing.get("fingerprint") == fingerprint and existing.get("status") in {"running", "completed"}:
        return existing
    task = _tasks.get(str(run.id))
    if task is not None and not task.done():
        return existing or {"status": "running", "model": "openai-compatible", "revision": revision, "fingerprint": fingerprint, "items": []}
    cache = {
        "status": "running",
        "model": "openai-compatible",
        "revision": revision,
        "fingerprint": fingerprint,
        "items": [],
        "started_at": _now(),
    }
    hints = dict(run.hints_json or {})
    hints[SEMANTIC_CACHE_KEY] = cache
    run.hints_json = hints
    await session.commit()
    task = asyncio.create_task(_run_background(str(run.id), fingerprint, revision, items), name=f"muteki-board-semantic-{run.id}")
    _tasks[str(run.id)] = task
    return cache


async def request_analysis_locked(session: AsyncSession, *, run: SolveRun, revision: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    async with _lock_for(str(run.id)):
        return await request_analysis(session, run=run, revision=revision, items=items)
