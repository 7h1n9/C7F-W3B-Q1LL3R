"""LLM-authored CTF Writeup sections for the canonical Muteki runtime.

Why this exists: the template renderer in :mod:`app.services.muteki_writeup` is
pure string assembly.  It can only restate the few lines the Worker chose to
promote to the Blackboard, so a solved Run still came out with

* a one-line "solution" copied verbatim from one fact,
* an "attack chain" that says nothing beyond ``自动化工具调用``,
* ``当前没有足够的已验证HTTP请求…`` instead of a reproducible script, and
* a remediation line templated off a guessed vulnerability class — "实施参数化
  查询…" for a model-inversion challenge.

The Worker's own CLI stream is the only place that knows HOW the flag was
actually reached.  It is captured per turn under ``evidence/cli-conversations``
(see ``muteki.solver.cli_driver.attach_conversation_sink``) and fed here, with
the deterministic Blackboard projection, to the Run's Reason model.  The model
writes the NARRATIVE sections only; every machine-verified section (metadata,
evidence chain, ledger, completion gate, tool list) stays generated in code, so
a hallucination can never be mistaken for evidence.

Never raises into the caller: any failure degrades to ``status="failed"`` and
the caller falls back to the deterministic renderer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm_parameters import FIELD_CONSTRAINT_RE, NEGOTIABLE_FIELDS, parameter_constraint
from app.models.model_config import ModelConfig
from app.schemas.model_config import OPENAI_WIRE_PROVIDER_TYPES

logger = logging.getLogger(__name__)

LLM_REPORT_CACHE_KEY = "muteki_llm_report"

# Model input budget.  A single bounded Intent routinely produces a 100–400 KB
# stream, and most of it is retries — so the tail (where the exploit lands)
# keeps twice the head's share.
_CONVERSATION_BUDGET = 180_000
# Below this the transcript only holds the completion stub — see build_llm_report.
_CONVERSATION_USEFUL_CHARS = 800
_TOOL_CALL_LIMIT = 40
_FACT_LIMIT = 40
_REQUEST_TIMEOUT_S = 240.0

# Parameters we send that one upstream may and another may not accept.  A
# rejection naming one of these is a PER-MODEL constraint, not a defect in our
# payload -- the same body succeeds on a different model, and usually on the
# same model once the value is adjusted.  Evidence: the Kimi relay answers
# ``400 {"error":{"message":"field temperature invalid, only 1 is allowed for
# this model","type":"invalid_request_error"}}``, i.e. it demands temperature=1
# while api.deepseek.com answers 200 for the identical body.
# The single implementation lives in ``app.core.llm_parameters`` because the
# Reason planner sees the very same relay error wrapped in a
# ``ModelProviderError``; both callers must negotiate it the same way.
_NEGOTIABLE_FIELDS = NEGOTIABLE_FIELDS

# ``field <name> invalid[, only <n> is allowed ...]`` -- the upstream tells us
# exactly which parameter it refuses, so there is nothing to guess.
_FIELD_CONSTRAINT_RE = FIELD_CONSTRAINT_RE

# Fields every OpenAI-wire endpoint accepts.  A rejection naming one of these
# really would fail identically on every candidate config.
_STRUCTURAL_DEFECT_TOKENS = ("messages is required", "response_format")

_SECRET_MASK = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|sessionid)\"?\s*[:=]\s*\"?([^\s,;}\"]+)",
    re.IGNORECASE,
)
_FLAG_MASK = re.compile(r"flag\{[^{}\r\n]*\}", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any, *, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _coerce_text(item: Any) -> str:
    """Flatten whatever the model put in a "list of strings" slot.

    The output schema asks for ``["GET /model", "POST /predict"]`` but models
    routinely emit ``[{"step": "..."}, {"step": "..."}]`` — well-formed JSON,
    wrong shape.  ``str()`` on that yields ``"{'step': 'GET /model'}"`` and the
    rendered report shows literal Python dicts, so unwrap the common single-key
    wrappers and only fall back to a compact JSON dump.
    """

    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for key in ("step", "text", "description", "content", "value", "detail", "title"):
            if key in item:
                inner = item[key]
                if isinstance(inner, str):
                    return inner
                return _coerce_text(inner)
        # No known key: keep it readable rather than showing a repr.
        return json.dumps(item, ensure_ascii=False, default=str)
    if isinstance(item, (list, tuple)):
        return " ".join(part for part in (_coerce_text(entry) for entry in item) if part)
    return str(item or "")


def _sanitize(text: str, *, keep_flags: bool = True, limit: int = 4000) -> str:
    """Strip credential-shaped values before anything leaves for the model."""

    body = str(text or "")
    body = _SECRET_MASK.sub(lambda m: f"{m.group(1)}=[REDACTED]", body)
    if not keep_flags:
        body = _FLAG_MASK.sub("flag{[REDACTED]}", body)
    return body[-limit:] if len(body) > limit else body


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n...[已截断 {len(text) - limit} 字符]...\n{text[-tail:]}"


# ── worker conversation loading ──────────────────────────────────────────────


def _conversation_files(workspace: Path) -> list[Path]:
    roots = (
        workspace / "evidence" / "cli-conversations",
        workspace / "evidence" / "native-workers",
    )
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".log", ".txt", ".json"}:
                found.append(path)
    return found


def load_worker_conversations(
    workspace: str | Path,
    *,
    budget: int = _CONVERSATION_BUDGET,
) -> dict[str, Any]:
    """Read every captured Worker transcript into one bounded, sanitized block."""

    root = Path(workspace)
    files = _conversation_files(root) if root.is_dir() else []
    index: list[dict[str, Any]] = []
    chunks: list[str] = []
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.strip():
            continue
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        index.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest[:16],
                "chars": len(raw),
            }
        )
        chunks.append(f"\n===== [{path.name}] =====\n{_sanitize(raw)}")

    if not chunks:
        return {"available": False, "files": [], "text": "", "chars": 0, "truncated": False}

    body = "\n".join(chunks)
    truncated = len(body) > budget
    if truncated:
        head = budget // 3
        body = (
            f"{body[:head]}\n...[对话中段已省略，保留开头与结尾（解题结论通常在结尾）]...\n"
            f"{body[-(budget - head):]}"
        )
    return {
        "available": True,
        "files": index,
        "text": body,
        "chars": len(body),
        "truncated": truncated,
    }


# ── structured model input ───────────────────────────────────────────────────


def _tool_rows(calls: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, call in enumerate(list(calls)[:_TOOL_CALL_LIMIT]):
        name = _text(_value(call, "tool_name", ""), limit=80)
        arguments = _value(call, "normalized_arguments") or _value(call, "arguments_json") or {}
        if not isinstance(arguments, Mapping):
            arguments = {"value": arguments}
        rows.append(
            {
                "index": index + 1,
                "tool": name,
                "status": _text(_value(call, "status", ""), limit=40),
                "arguments": _sanitize(
                    json.dumps(arguments, ensure_ascii=False, default=str), limit=900
                ),
            }
        )
    return rows


def _fact_rows(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list(facts)[:_FACT_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {
                "sequence": item.get("sequence"),
                "actor": _text(item.get("actor"), limit=40),
                "verified": bool(item.get("verified")),
                "content": _sanitize(_text(item.get("content") or item.get("summary_zh"), limit=600)),
                "evidence_refs": [str(ref)[:24] for ref in (item.get("evidence_refs") or [])][:4],
            }
        )
    return rows


def build_model_input(
    *,
    challenge: Any,
    run: Any,
    graph_state: Mapping[str, Any],
    calls: Sequence[Any],
    evidence: Sequence[Any],
    conversations: Mapping[str, Any],
    poc_available: bool,
    verified_flag: str,
    usage_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the bounded (never raw-secret) payload handed to the model."""

    facts = [item for item in (graph_state.get("facts") or []) if isinstance(item, Mapping)]
    dead_ends = [item for item in (graph_state.get("dead_ends") or []) if isinstance(item, Mapping)]
    intents = [item for item in (graph_state.get("intents") or []) if isinstance(item, Mapping)]
    started = _value(run, "started_at", None)
    finished = _value(run, "finished_at", None)
    duration = ""
    try:
        if started and finished:
            duration = f"{round((finished - started).total_seconds(), 1)}s"
    except Exception:
        duration = ""
    tool_rows = _tool_rows(calls)
    tool_usage: dict[str, int] = {}
    for row in tool_rows:
        name = row["tool"] or "unknown"
        tool_usage[name] = tool_usage.get(name, 0) + 1
    return {
        "challenge": {
            "name": _text(_value(challenge, "name", "") or _value(challenge, "title", ""), limit=200),
            "target_url": _text(_value(challenge, "target_url", ""), limit=300),
            "category": _text(_value(challenge, "category", ""), limit=60),
            "description": _sanitize(_text(_value(challenge, "description", ""), limit=800)),
            # The flag shape is authoritative here and is what Workers were told
            # to match.  Without it the model invents the near-universal
            # ``flag{...}`` default and writes a PoC with the wrong prefix.
            "flag_pattern": _text(_value(challenge, "flag_pattern", ""), limit=200),
        },
        "run": {
            "status": _text(_value(run, "status", ""), limit=40),
            "solver_mode": _text(_value(run, "solver_mode", ""), limit=40),
            "duration": duration,
            "tool_call_count": len(tool_rows),
            "tool_usage": tool_usage,
        },
        "usage": dict(usage_summary or {}),
        "board": {
            "revision": graph_state.get("revision"),
            "intents": [
                {
                    "id": _text(item.get("id"), limit=40),
                    "status": _text(item.get("status"), limit=40),
                    "result": _text(item.get("result"), limit=80),
                    "detail": _sanitize(_text(item.get("detail") or item.get("description"), limit=300)),
                }
                for item in intents[:20]
            ],
            "facts": _fact_rows(facts),
            "dead_ends": [
                {"description": _sanitize(_text(item.get("description"), limit=400))}
                for item in dead_ends[:20]
            ],
            "verified_flag": _text(verified_flag, limit=200),
            # Ordered events are the fallback trace when no CLI transcript was
            # captured: they still say who did what, in order.
            "timeline": [
                {
                    "seq": item.get("sequence"),
                    "actor": _text(item.get("actor"), limit=40),
                    "kind": _text(item.get("kind"), limit=40),
                    "verified": bool(item.get("verified")),
                    "detail": _sanitize(
                        _text(
                            json.dumps(item.get("detail") or {}, ensure_ascii=False, default=str),
                            limit=500,
                        )
                    ),
                }
                for item in (graph_state.get("events") or [])[:80]
            ],
        },
        "tool_calls": _tool_rows(calls),
        "evidence": [
            {
                "id": str(_value(item, "id", ""))[:40],
                "type": _text(_value(item, "evidence_type", ""), limit=60),
                "status": _text(_value(item, "status", ""), limit=30),
                "summary": _sanitize(_text(_value(item, "summary", ""), limit=300)),
            }
            for item in list(evidence)[:30]
        ],
        "poc_bundle_available": bool(poc_available),
        "worker_conversation": {
            "available": bool(conversations.get("available")),
            "files": list(conversations.get("files") or [])[:40],
            "chars": conversations.get("chars", 0),
            "truncated": bool(conversations.get("truncated")),
            "text": str(conversations.get("text") or "")[:_CONVERSATION_BUDGET],
        },
    }


def report_fingerprint(model_input: Mapping[str, Any]) -> str:
    payload = json.dumps(model_input, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


# ── prompt / schema ──────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "你是 CTF Writeup 撰写助手，为一道已完成的授权靶场题目写中文报告章节。\n"
    "输入包含两块：① Run 的确定性结果（黑板已验证事实、dead end、工具调用、Evidence 引用、是否含flag）；"
    "② Worker Agent 的完整对话/命令输出原文（可能很长且被截断）。\n"
    "规则：\n"
    "1. 只对证据中出现的内容下笔。若某个技术细节在输入里找不到依据，不要编造，"
    "在 notes 里写明「证据不足」和缺什么。\n"
    "2. 优先依据 Worker 对话还原真实过程（它做了什么命令、发了什么请求、观察到什么、如何改策略），"
    "再用黑板事实交叉核对。\n"
    "3. repro 必须是可独立执行的：给出具体的 HTTP 方法、路径、参数、请求体，"
    "必要时给出完整 Python/curl 脚本；不要写「重复上述步骤」这类空话。\n"
    "3a. 当 worker_conversation.available 为 false（没有 Worker 对话原文）时，"
    "你不知道接口的确切响应字段名/结构。此时 repro 必须是「发现式」的："
    "第一步先请求接口并打印原始响应原文，再依据打印结果取字段；"
    "任何凭猜想的字段名都要在 notes 里显式声明为未验证猜测，不得写成确定事实。\n"
    "4. remediation 必须针对本题实际漏洞类型给出可落地措施，禁止套用与本题无关的通用话术"
    "（参数化查询、越权校验等仅在确为相关漏洞时使用）。\n"
    "5. 输出严格 JSON，不要 Markdown 代码块、不要解释、不要密码/Cookie/Token 原值。\n"
)

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_liner", "vuln_type", "attack_chain", "execution", "repro", "failed_paths", "remediation"],
    "properties": {
        "one_liner": {"type": "string", "maxLength": 400},
        "vuln_type": {"type": "string", "maxLength": 60},
        "attack_chain": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 300},
        },
        "execution": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step", "title", "detail"],
                "properties": {
                    "step": {"type": "integer"},
                    "title": {"type": "string", "maxLength": 120},
                    "detail": {"type": "string", "maxLength": 1200},
                },
            },
        },
        "repro": {"type": "string", "maxLength": 6000},
        "failed_paths": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 400},
        },
        "remediation": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 400},
        },
        "notes": {"type": "string", "maxLength": 600},
    },
}


def _user_prompt(model_input: Mapping[str, Any]) -> str:
    payload = json.dumps(model_input, ensure_ascii=False, separators=(",", ":"), default=str)
    return (
        "下面是本次 Run 的确定性结果和 Worker 对话原文（JSON）。\n"
        "**除 vuln_type 外，所有输出字段一律使用简体中文**；"
        "命令、脚本、URL、HTTP 头、工具名、接口字段名等技术标识保留英文原样，不必翻译。\n"
        "输出字段：one_liner（≤120 字，一句话解法，含关键入口/参数/结论）、"
        "vuln_type（简短英文大类，如 SQLI / IDOR / SSTI / MODEL_INVERSION / LOGIC_FLAW）、"
        "attack_chain（3-8 步，每步 ≤80 字，描述「侦察 → 发现 → 利用 → 取 Flag」链路）、"
        "execution（按时间顺序 3-10 步真实执行过程，每步含 title 与 detail，"
        "detail 要带具体命令/请求/观察到的现象）、"
        "repro（Markdown：环境要求 + 可执行 curl 或 Python 脚本 + 预期输出/flag 位置；"
        "说明性文字用中文，脚本与命令本身保持原样）、"
        "failed_paths（走不通的路线与原因）、"
        "remediation（3-6 条针对性修复建议）、notes（证据缺口说明，无则空字符串）。\n"
        "硬约束：flag 前缀必须以 challenge.flag_pattern 为准（例如 CTF2\\{...\\} 而非想当然的 flag\\{...\\}）；"
        "repro 脚本里的接口字段名/路径必须来自 worker_conversation 中真实出现过的请求与响应，"
        "不得臆造字段，无法确定的字段要在脚本里先打印原始响应再解析。\n"
        f"输入数据：{payload}"
    )


# ── model call ───────────────────────────────────────────────────────────────


def _usable_config(config: Any) -> bool:
    """A config is usable only if it is enabled, openai-wire, and fully credentialed."""

    return bool(
        config is not None
        and getattr(config, "enabled", False)
        and getattr(config, "provider_type", None) in OPENAI_WIRE_PROVIDER_TYPES
        and getattr(config, "encrypted_api_key", None)
        and getattr(config, "base_url", None)
    )


async def _resolve_config_candidates(
    session: AsyncSession, run: Any
) -> list[ModelConfig]:
    """Ordered list of configs to try: the Run's Reason model first, then peers.

    Ordered rather than single because a stored ``model_name`` can go stale
    independently of the config row — an upstream proxy starts returning
    ``model [x] service info not found`` while the row still looks healthy
    (enabled, keyed, wired).  One dead name must not cost the whole section, so
    the caller walks this list and keeps the first config whose model answers.
    """

    from app.solver.muteki.runtime.configuration import runtime_selection_from_hints

    reason_id, _ = runtime_selection_from_hints(dict(getattr(run, "hints_json", None) or {}))
    ordered: list[Any] = []
    seen: set[str] = set()

    def _push(candidate: Any) -> None:
        if not _usable_config(candidate):
            return
        key = str(getattr(candidate, "id", "") or "")
        if key and key in seen:
            return
        seen.add(key)
        ordered.append(candidate)

    if reason_id:
        _push(await session.get(ModelConfig, reason_id))
    peers = (
        await session.scalars(
            select(ModelConfig)
            .where(
                ModelConfig.enabled,
                ModelConfig.provider_type.in_(sorted(OPENAI_WIRE_PROVIDER_TYPES)),
            )
            .order_by(ModelConfig.created_at)
        )
    ).all()
    for peer in peers:
        _push(peer)
    if not ordered:
        raise ValueError("LLM_REPORT_MODEL_UNAVAILABLE")
    return ordered


async def _resolve_config(session: AsyncSession, run: Any) -> tuple[ModelConfig, str, str]:
    """Pick the Run's Reason model, falling back to the first enabled openai-wire config."""

    from app.services.crypto import decrypt_api_key

    config = (await _resolve_config_candidates(session, run))[0]
    return config, str(config.model_name or config.name), decrypt_api_key(config.encrypted_api_key)


def _salvage_truncated(text: str) -> Any | None:
    """Recover what a length-capped model response did manage to emit.

    ``finish_reason == "length"`` means the model hit ``max_tokens`` mid-string:
    the JSON is syntactically cut in half and no escape repair can help — the
    closing quote is simply absent.  The lead sections (``one_liner``,
    ``attack_chain``, the first ``execution`` steps) are complete and useful, so
    salvage them instead of discarding the whole report:

    1. *Clean cut* — the stream happened to end exactly between values, so
       re-closing the open brackets parses as-is.
    2. *Mid-string cut* — close the dangling string, then close every bracket
       still open, innermost first, and let the parser drop the broken tail.
    """

    if not text.startswith("{"):
        return None

    # 1. Clean cut: the emitter stopped on a token boundary.
    for suffix in ("}", '"}', "}]", '"}]', "]}", '"]}', '"}]}'):
        try:
            return json.loads(text + suffix)
        except ValueError:
            continue

    # 2. Mid-string cut: track bracket nesting outside strings so a brace that
    #    is *text* (repro scripts are full of them) is not mistaken for syntax.
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    closing = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    candidate = text + ('"' if in_string else "") + closing
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _loads_tolerant(text: str) -> Any:
    """Parse model JSON, repairing the invalid escapes models routinely emit.

    The narrative input is a Worker transcript, which is itself full of
    backslashes (``\\n`` in JSON-lines streams, ``\\d`` in regexes, Windows
    paths, escaped quotes).  A model narrating that transcript will sometimes
    reproduce one unescaped, and a strict ``json.loads`` then rejects the whole
    document — throwing away a perfectly good report over one byte.  Repair the
    two defects that actually occur (unknown escape sequences, raw control
    characters inside strings) instead of failing the section.
    """

    try:
        return json.loads(text)
    except ValueError:
        pass
    repaired = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
    for candidate in (repaired, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", repaired)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    # Truncated response (max_tokens hit mid-JSON): the escapes are already
    # repaired, so only the tail is broken — salvage the complete head.
    salvaged = _salvage_truncated(repaired)
    if salvaged is not None:
        return salvaged
    raise ValueError("LLM_REPORT_JSON_UNPARSEABLE")


def _parse_message(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction (some proxies ignore ``response_format``)."""

    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    if not text:
        raise ValueError("LLM_REPORT_EMPTY_RESPONSE")
    start = text.find("{")
    if start > 0:
        text = text[start:]
    # Only trim prose AROUND the object when the object is actually complete.
    # Slicing at ``rfind('}')`` unconditionally would, for a response cut off at
    # ``max_tokens``, land on the last brace of the last complete value (a repro
    # script is full of them) and silently discard the entire ``repro`` section.
    close = text.rfind("}")
    if close > 0 and not text[close + 1 :].strip().strip("`").strip():
        text = text[: close + 1]
    document = _loads_tolerant(text)
    if not isinstance(document, dict):
        raise ValueError("LLM_REPORT_SHAPE_INVALID")

    def _list_of_str(key: str, limit: int) -> list[str]:
        rows = []
        for item in list(document.get(key) or [])[:limit]:
            row = _sanitize(_text(_coerce_text(item), limit=500))
            if row:
                rows.append(row)
        return rows

    execution: list[dict[str, Any]] = []
    for index, item in enumerate(list(document.get("execution") or [])[:20]):
        if not isinstance(item, Mapping):
            continue
        title = _sanitize(_text(item.get("title"), limit=120))
        detail = _sanitize(_text(item.get("detail"), limit=1200))
        if not (title or detail):
            continue
        try:
            step = int(item.get("step") or (index + 1))
        except (TypeError, ValueError):
            step = index + 1
        execution.append({"step": step, "title": title, "detail": detail})

    sections = {
        "one_liner": _sanitize(_text(document.get("one_liner"), limit=400)),
        "vuln_type": _text(document.get("vuln_type"), limit=60).upper(),
        "attack_chain": _list_of_str("attack_chain", 10),
        "execution": execution,
        "repro": _sanitize(str(document.get("repro") or "")[:6000]),
        "failed_paths": _list_of_str("failed_paths", 12),
        "remediation": _list_of_str("remediation", 8),
        "notes": _sanitize(_text(document.get("notes"), limit=600)),
    }
    return sections


async def _post_completion(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    api_key: str,
    model_input: Mapping[str, Any],
    payload_overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(model_input)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    for key, value in (payload_overrides or {}).items():
        # ``None`` means "omit the field" -- see _parameter_constraint.
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    response = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    response.raise_for_status()
    body = response.json()
    message = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = dict(body.get("usage") or {})
    sections = _parse_message(message)
    if not sections["one_liner"] and not sections["execution"] and not sections["repro"]:
        raise ValueError("LLM_REPORT_NO_SECTIONS")
    return sections, {
        "model": model,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _is_model_unavailable(error: Exception) -> bool:
    """True when the upstream says the *model name* is dead, not the request."""

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {400, 403, 404, 410, 422, 503}:
            body = str(getattr(error.response, "text", "") or "")
            if "model" in body.lower() and (
                "not found" in body.lower()
                or "not available" in body.lower()
                or "not supported" in body.lower()
                or "not exist" in body.lower()
                or "unavailable" in body.lower()
            ):
                return True
        return False
    return "LLM_REPORT_MODEL_UNAVAILABLE" in str(error)


def _parameter_constraint(error: Exception) -> tuple[str, float | None] | None:
    """``(field, allowed_value)`` when the upstream refuses ONE parameter we send.

    Relays wrap the vendor's error but keep its wording, so the offending
    parameter is still named: ``field temperature invalid, only 1 is allowed for
    this model``.  That is a fact about that model, not about our payload, so
    the caller retries the same config with the field adjusted instead of
    writing the whole narrative section off as unusable.

    ``allowed_value`` is ``None`` when the message names the field but offers no
    replacement -- the caller then drops the field entirely.
    """

    # Delegated so the writeup and the Reason planner cannot drift apart.
    return parameter_constraint(error)


def _is_payload_defect(error: Exception) -> bool:
    """True ONLY when the failure is unambiguously a defect in OUR payload.

    The request body is byte-identical for every candidate config, so a genuine
    payload defect would fail everywhere; retrying would just burn the remaining
    providers.  Everything else belongs to ONE config — a dead model name, an
    expired key, a dead endpoint, a rate limit, a proxy error page, a model that
    emitted unparseable JSON, or a per-model parameter limit — and the next
    candidate may well answer.

    This must stay conservative, because a false "payload defect" aborts the
    whole report.  Two outages proved it:

    * a broken relay returned an HTML Cloudflare "524: A timeout occurred" page
      with HTTP 400 — its body never mentions ``model``, so the old "not a model
      problem → fail fast" rule killed the report while four healthy configs sat
      unused; and
    * the Kimi relay returns ``invalid_request_error`` for ``temperature: 0.2``
      — which the old token list counted as OUR defect, aborting the walk even
      though api.deepseek.com answers the identical body with 200.  A per-model
      parameter limit is not a payload defect; it is negotiated (see
      :func:`_parameter_constraint`) or simply walked past.

    What remains structural — and therefore genuinely fatal on every config — is
    a rejection of a field that every OpenAI-wire endpoint accepts.
    """

    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return False  # infrastructure, not payload
    if _parameter_constraint(error) is not None:
        return False  # one model's parameter limit, not a malformed request
    if not isinstance(error, httpx.HTTPStatusError):
        # Includes the parse sentinels (LLM_REPORT_JSON_UNPARSEABLE,
        # LLM_REPORT_NO_SECTIONS, LLM_REPORT_EMPTY_RESPONSE).  Those are model
        # OUTPUT problems and model output is non-deterministic — a retry on a
        # different model is exactly the right response.
        return False
    body = str(getattr(error.response, "text", "") or "")
    # An HTML body is a proxy/edge error page, never an API validation error.
    if not body.lstrip().startswith("{"):
        return False
    status = error.response.status_code
    if status not in {400, 404, 410, 422}:
        return False  # 401/403/408/429/5xx are endpoint or auth, not payload
    lowered = body.lower()
    # Only an API error naming a field every endpoint accepts proves the payload
    # is wrong.
    return any(token in lowered for token in _STRUCTURAL_DEFECT_TOKENS)


def _should_try_next_config(error: Exception) -> bool:
    """Should the caller advance to the next candidate config?"""

    return not _is_payload_defect(error)


async def call_report_model(
    session: AsyncSession,
    run: Any,
    model_input: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the report model, walking configs until one answers.

    A stale ``model_name`` or a broken relay is a configuration/infrastructure
    failure with no code change to blame, so it must not silently blank the
    narrative section.  Trying the next usable config turns a hard failure into a
    working report; only a defect that is genuinely in our own payload — which
    would fail identically everywhere — stops the walk early.

    Each config gets at most two attempts: the second repeats the call with the
    one parameter the upstream itself named as out of range.  That recovery is
    per-config on purpose — "temperature must be 1" is a property of that relay,
    and dropping it for the whole walk would quietly change every other model's
    sampling for no reason.
    """

    from app.services.crypto import decrypt_api_key

    candidates = await _resolve_config_candidates(session, run)
    timeout = httpx.Timeout(connect=10, read=_REQUEST_TIMEOUT_S, write=10, pool=10)
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for index, config in enumerate(candidates):
            model = str(config.model_name or config.name)
            base_url = str(config.base_url).rstrip("/")
            api_key = decrypt_api_key(config.encrypted_api_key)
            overrides: dict[str, Any] | None = None
            for attempt in range(2):
                try:
                    sections, usage = await _post_completion(
                        client,
                        base_url=base_url,
                        model=model,
                        api_key=api_key,
                        model_input=model_input,
                        payload_overrides=overrides,
                    )
                except Exception as error:  # noqa: BLE001 - fall through to next candidate
                    last_error = error
                    constraint = _parameter_constraint(error) if attempt == 0 else None
                    if constraint is not None:
                        field, allowed = constraint
                        overrides = {field: allowed if allowed is not None else None}
                        logger.warning(
                            "LLM report config %s (model %s) rejected the %s we sent; "
                            "retrying with %s",
                            getattr(config, "name", ""),
                            model,
                            field,
                            f"{allowed}" if allowed is not None else "the field omitted",
                        )
                        continue
                    if index + 1 < len(candidates) and _should_try_next_config(error):
                        logger.warning(
                            "LLM report config %s (model %s at %s) failed: %s; trying the next config",
                            getattr(config, "name", ""),
                            model,
                            base_url,
                            str(error)[:200],
                        )
                        break
                    raise
                if index:
                    logger.info(
                        "LLM report fell back to config %s (model %s)",
                        getattr(config, "name", ""),
                        model,
                    )
                return sections, usage
    if last_error is not None:
        raise last_error
    raise ValueError("LLM_REPORT_MODEL_UNAVAILABLE")


# ── public entry point ───────────────────────────────────────────────────────


async def build_llm_report(
    session: AsyncSession,
    *,
    run: Any,
    challenge: Any,
    graph_state: Mapping[str, Any],
    calls: Sequence[Any],
    evidence: Sequence[Any],
    poc_available: bool,
    verified_flag: str = "",
    usage_summary: Mapping[str, Any] | None = None,
    timeout: float = _REQUEST_TIMEOUT_S + 30.0,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Author the narrative Writeup sections for one completed Muteki Run.

    Result is cached on ``run.hints_json[LLM_REPORT_CACHE_KEY]`` keyed by an
    input fingerprint, so re-rendering the report (export, UI re-render) never
    pays for a second model call.  Callers pass a live ``session``; nothing is
    committed here — the caller owns its transaction.
    """

    workspace = str(getattr(run, "workspace_path", "") or "")
    conversations = await asyncio.to_thread(load_worker_conversations, workspace)
    # A stub ("official CliSolver completed one bounded Intent", 47 chars) counts
    # as AVAILABLE on disk but carries nothing. Calling that "Worker 对话" would
    # let the report claim a provenance it does not have, so anything below the
    # usefulness floor is reported as missing — the model is then told it must
    # reconstruct from the board instead of quoting a transcript.
    if conversations.get("available") and (conversations.get("chars") or 0) < _CONVERSATION_USEFUL_CHARS:
        conversations = {**conversations, "available": False, "reason": "conversation_too_short"}
    model_input = build_model_input(
        challenge=challenge,
        run=run,
        graph_state=graph_state,
        calls=calls,
        evidence=evidence,
        conversations=conversations,
        poc_available=poc_available,
        verified_flag=verified_flag,
        usage_summary=usage_summary,
    )
    fingerprint = report_fingerprint(model_input)
    cache: dict[str, Any] = {}
    if use_cache:
        cache = dict((dict(getattr(run, "hints_json", None) or {}).get(LLM_REPORT_CACHE_KEY)) or {})
        if cache.get("fingerprint") == fingerprint and cache.get("status") == "completed":
            return cache

    base_cache = {
        "status": "running",
        "fingerprint": fingerprint,
        "conversation_available": bool(conversations.get("available")),
        "conversation_chars": int(conversations.get("chars") or 0),
        "started_at": _now(),
    }
    try:
        sections, usage = await asyncio.wait_for(
            call_report_model(session, run, model_input), timeout=timeout
        )
    except TimeoutError:
        logger.warning("LLM writeup timed out for run %s", getattr(run, "id", "?"))
        result = {**base_cache, "status": "failed", "error_code": "LLM_REPORT_TIMEOUT", "sections": {}, "updated_at": _now()}
    except Exception as error:  # noqa: BLE001 - degraded mode is the contract
        code = str(error).strip() or type(error).__name__.upper()
        logger.warning("LLM writeup failed for run %s: %s", getattr(run, "id", "?"), code)
        result = {
            **base_cache,
            "status": "failed",
            "error_code": code[:160],
            "sections": {},
            "model": (cache.get("model") if isinstance(cache, Mapping) else "") or "",
            "updated_at": _now(),
        }
    else:
        result = {
            **base_cache,
            "status": "completed",
            "sections": sections,
            "usage": usage,
            "model": usage.get("model", ""),
            "updated_at": _now(),
        }

    hints = dict(getattr(run, "hints_json", None) or {})
    hints[LLM_REPORT_CACHE_KEY] = result
    run.hints_json = hints
    return result


__all__ = [
    "LLM_REPORT_CACHE_KEY",
    "build_llm_report",
    "build_model_input",
    "load_worker_conversations",
    "report_fingerprint",
]