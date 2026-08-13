"""Read-only projection of the official Muteki SharedGraph.

The native graph is the Blackboard authority for a Muteki Run.  This module
is deliberately an observer: it opens the run-scoped SQLite graph in
``mode=ro`` and returns safe, UI/API-shaped data without replaying or mutating
the event log.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

from ..upstream_bridge import to_upstream_challenge
from .upstream_events import project_upstream_events


_KEY_CONDITION_EXCLUSIONS = (
    "hypothesis",
    "candidate",
    "inconclusive",
    "review_needed",
    "unverified",
    "baseline",
    "dead end",
    "待验证",
    "候选",
    "假设",
    "未确认",
)


def _evidence_refs(detail: dict[str, Any], event: dict[str, Any]) -> list[str]:
    """Return only durable evidence references, never raw tool output."""

    refs: list[str] = []
    raw_refs = detail.get("evidence_refs")
    if isinstance(raw_refs, list):
        refs.extend(str(item) for item in raw_refs if str(item).strip())
    artifact_id = event.get("artifact_id")
    if artifact_id and str(artifact_id) not in refs:
        refs.append(str(artifact_id))
    return refs


def _is_key_condition(content: str) -> bool:
    """Keep concise, evidence-worthy facts for the investigation board.

    The official graph remains the authority for whether a fact is active and
    verified.  This second, presentation-only filter removes planning noise
    such as hypotheses and baseline notes without changing the graph or audit
    history.
    """

    normalized = re.sub(r"\s+", " ", content).strip().casefold()
    if not normalized or any(marker in normalized for marker in _KEY_CONDITION_EXCLUSIONS):
        return False
    return bool(
        re.search(
            r"(?:https?://|/[-\w]|\b(?:account|username|password|credential|token|cookie|session|endpoint|route|parameter|vulnerability|injection|idor|traversal|admin|login|jwt|secret|file|api|flag)\b|账号|用户名|密码|凭据|接口|路径|参数|漏洞|越权|会话|登录|密钥|文件|数据库|工单|审批)",
            normalized,
            re.IGNORECASE,
        )
    )


def _summary_zh(content: str) -> str:
    """Create a deterministic, structured Chinese board label.

    This is intentionally presentation-only and does not call an LLM.  The
    common Muteki fact shapes are reduced to short, readable conclusions while
    preserving technical anchors such as methods, paths and status codes.
    """

    value = re.sub(r"\s+", " ", content).strip()
    if not value:
        return ""

    # Structured HTTP observations are common in the graph.  Keep them
    # readable instead of exposing JSON punctuation on the board.
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("endpoint"):
        method = str(parsed.get("method") or "GET").upper()
        endpoint = str(parsed.get("endpoint") or "")
        status = parsed.get("status_code")
        status_text = f"HTTP {status}" if status is not None else "状态未知"
        if status in {401, 403}:
            conclusion = "需要认证"
        elif isinstance(status, int) and status < 400:
            conclusion = "请求成功"
        elif status == 404:
            conclusion = "端点不存在"
        else:
            conclusion = "请求结果需复核"
        return f"接口：{method} {endpoint}；{status_text}，{conclusion}。"

    # Facts emitted by Codex workers use a small set of recurring evidence
    # sentence shapes.  Convert those shapes into Chinese findings rather than
    # performing a lossy word-by-word translation.
    value = re.sub(r"^\[(?:codex|worker[^]]*)\]\s*", "", value, flags=re.IGNORECASE)

    method_path_status = re.search(
        r"\b(GET|POST|PUT|PATCH|DELETE)\s+(`?/[\w./?=&<>:-]+`?).*?"
        r"(?:without authentication|unauthenticated).*?"
        r"(?:HTTP\s*)?(\d{3})",
        value,
        flags=re.IGNORECASE,
    )
    if method_path_status:
        method, endpoint, status = method_path_status.groups()
        return f"接口：{method.upper()} {endpoint.strip('`')}；未登录请求稳定返回 HTTP {status}，认证边界已确认。"

    dynamic_routes = re.search(
        r"Dynamic routes?\s+(.+?)\s+exist;\s*unauthenticated\s+GET\s+returns\s+`?(\d{3})`?",
        value,
        flags=re.IGNORECASE,
    )
    if dynamic_routes:
        routes = ", ".join(re.findall(r"`(/[^`]+)`", dynamic_routes.group(1)))
        routes = routes or dynamic_routes.group(1).strip()
        return f"动态路由：{routes}；未登录 GET 返回 HTTP {dynamic_routes.group(2)}。"

    root_links = re.search(
        r"Root page exposes links to\s+(.+?),\s*plus\s+POST\s+(`?/[\w./?=&<>:-]+`?)",
        value,
        flags=re.IGNORECASE,
    )
    if root_links:
        routes = ", ".join(re.findall(r"`(/[^`]+)`", root_links.group(1)))
        routes = routes or root_links.group(1).strip()
        return f"首页公开入口：{routes}；登录接口：POST {root_links.group(2).strip('`')}。"

    post_discovery = re.search(
        r"Focused POST discovery found no additional .*? beyond\s+(.+?);\s*POST to the GET-only linked pages returns HTTP\s*(\d{3})",
        value,
        flags=re.IGNORECASE,
    )
    if post_discovery:
        routes = ", ".join(re.findall(r"`(/[^`]+)`", post_discovery.group(1)))
        routes = routes or post_discovery.group(1).strip()
        return f"POST 侦察未发现新增接口（已知：{routes}）；对 GET-only 页面使用 POST 返回 HTTP {post_discovery.group(2)}。"

    identical_pages = re.search(
        r"Unauthenticated GETs to\s+(.+?)\s+all returned byte-identical\s+(\d+)-byte HTML",
        value,
        flags=re.IGNORECASE,
    )
    if identical_pages:
        routes = ", ".join(re.findall(r"`(/[^`]+)`", identical_pages.group(1)))
        routes = routes or identical_pages.group(1).strip()
        return f"未登录页面：{routes} 均返回相同的 {identical_pages.group(2)} 字节 HTML，说明统一受登录页保护。"

    unauthenticated_links = re.search(
        r"The only unauthenticated links found .*? are\s+(.+?);\s*no JavaScript",
        value,
        flags=re.IGNORECASE,
    )
    if unauthenticated_links:
        routes = ", ".join(re.findall(r"`(/[^`]+)`", unauthenticated_links.group(1)))
        routes = routes or unauthenticated_links.group(1).strip()
        return f"未登录可见入口：{routes}；未发现 JavaScript 或内联脚本。"

    if re.search(r"No employee profile table.*?visible without login", value, flags=re.IGNORECASE):
        return "未登录页面未泄露员工资料、文件列表、上传/预览下载信息或 Flag。"

    credentials = re.search(
        r"(?:demo credentials|demo credential)\s+([\w.-]+)\s*/\s*([\w.-]+)",
        value,
        flags=re.IGNORECASE,
    )
    if credentials:
        return f"演示登录凭据：账号 {credentials.group(1)}，密码 {credentials.group(2)}。"

    replacements = (
        (r"\bsql\s+injection\b", "SQL 注入"),
        (r"\bpath\s+traversal\b", "路径遍历"),
        (r"\bcommand\s+injection\b", "命令注入"),
        (r"\bconfirmed\b", "已确认"),
        (r"\bverified\b", "已验证"),
        (r"\bendpoint\b", "接口"),
        (r"\broute\b", "路径"),
        (r"\bparameter\b", "参数"),
        (r"\busername\b", "用户名"),
        (r"\bpassword\b", "密码"),
        (r"\bcredential(?:s)?\b", "凭据"),
        (r"\bsession\b", "会话"),
        (r"\bcookie\b", "Cookie"),
        (r"\blogin\b", "登录"),
        (r"\baccount\b", "账号"),
        (r"\badmin\b", "管理员"),
        (r"\bfile\b", "文件"),
        (r"\bapi\b", "API"),
        (r"\bflag\b", "Flag"),
        (r"\breturned\b", "返回"),
        (r"\bresponse\b", "响应"),
        (r"\bstatus\b", "状态"),
        (r"\bwithout authentication\b", "未登录"),
        (r"\bunauthenticated\b", "未登录"),
        (r"\bconsistently\b", "稳定"),
        (r"\bexists?\b", "存在"),
        (r"\badditional\b", "额外"),
        (r"\bdiscovery\b", "侦察"),
    )
    translated = value
    for pattern, replacement in replacements:
        translated = re.sub(pattern, replacement, translated, flags=re.IGNORECASE)
    return translated if re.search(r"[\u4e00-\u9fff]", translated) else f"已确认条件：{translated}"


def read_native_graph_snapshot(
    *,
    db_path: str | Path,
    challenge: Any,
    run_id: str,
) -> dict[str, Any]:
    """Return the current official Blackboard projection for one Run.

    Missing or unreadable graphs are reported as an unavailable observer
    state.  Callers must not fall back to opening the graph read-write here:
    observing a live Run must never create schema, advance revisions, or
    compete with the Coordinator's writer connection.
    """

    path = Path(db_path)
    if not path.is_file():
        return {
            "available": False,
            "run_id": str(run_id),
            "revision": 0,
            "facts": [],
            "key_conditions": [],
            "intents": [],
            "dead_ends": [],
            "flags": [],
            "pocs": [],
            "reason": "GRAPH_NOT_INITIALIZED",
        }

    from muteki.swarm.shared_graph import SQLiteSharedGraph

    # Avoid touching ORM relationship attributes from the observer thread.
    # The official graph only needs the immutable public challenge contract.
    graph_challenge = SimpleNamespace(
        id=str(getattr(challenge, "id", "") or ""),
        name=str(getattr(challenge, "name", "") or ""),
        challenge_type=str(getattr(challenge, "challenge_type", "WEB_TARGET") or "WEB_TARGET"),
        description=str(getattr(challenge, "description", "") or ""),
        target_url=str(getattr(challenge, "target_url", "") or "") or None,
        flag_pattern=str(getattr(challenge, "flag_pattern", r"flag\{.*?\}") or r"flag\{.*?\}"),
        attachments=[],
    )
    graph = SQLiteSharedGraph.open_readonly(
        db_path=path,
        challenge=to_upstream_challenge(graph_challenge),
    )
    try:
        raw_events = graph.events_since(0)
        fact_states = graph._fact_state_map() if hasattr(graph, "_fact_state_map") else {}
    finally:
        graph.close()

    events = project_upstream_events(raw_events, challenge_id=str(run_id))
    raw_events_by_sequence = {
        int(item.get("seq") or 0): item for item in raw_events
    }
    facts: list[dict[str, Any]] = []
    key_conditions: list[dict[str, Any]] = []
    intents: dict[str, dict[str, Any]] = {}
    dead_ends: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    pocs: dict[str, dict[str, Any]] = {}

    for event in events:
        payload = dict(event.payload or {})
        kind = str(payload.get("upstream_event_type") or "")
        detail = payload.get("payload")
        detail = dict(detail) if isinstance(detail, dict) else {}
        sequence = int(event.sequence or 0)
        if kind == "fact_added":
            refs = _evidence_refs(detail, raw_events_by_sequence.get(sequence, {}))
            state = fact_states.get(sequence, {})
            effective_verified = state.get("verified_effective")
            verified = bool(event.verified) if effective_verified is None else bool(effective_verified)
            facts.append(
                {
                    "sequence": sequence,
                    "content": str(detail.get("fact") or detail.get("content") or ""),
                    "verified": verified,
                    "evidence_refs": refs,
                }
            )
            content = facts[-1]["content"]
            state_name = str(state.get("state") or "unresolved")
            if (
                verified
                and not state.get("retired")
                and state_name not in {"rejected", "merged", "superseded"}
                and refs
                and _is_key_condition(content)
            ):
                key_conditions.append(
                    {
                        "sequence": sequence,
                        "content": content,
                        "summary_zh": _summary_zh(content),
                        "verified": True,
                        "evidence_refs": refs,
                        "confidence": state.get("confidence_effective", event.confidence),
                        "source_worker_id": event.actor,
                    }
                )
        elif kind == "intent_proposed":
            intent_id = str(detail.get("intent_id") or f"intent-{sequence}")
            intents[intent_id] = {
                "id": intent_id,
                "description": str(detail.get("goal") or detail.get("description") or ""),
                "status": "open",
                "worker": None,
                "sequence": sequence,
            }
        elif kind in {"intent_claimed", "intent_concluded", "intent_released", "intent_state_changed"}:
            intent_id = str(detail.get("intent_id") or "")
            if not intent_id:
                continue
            current = intents.setdefault(
                intent_id,
                {"id": intent_id, "description": "", "status": "open", "worker": None, "sequence": sequence},
            )
            if kind == "intent_claimed":
                current.update({"status": "claimed", "worker": str(event.actor or "")})
            elif kind == "intent_concluded":
                current.update({"status": "done", "result": str(detail.get("result") or "")})
            elif kind == "intent_released":
                current.update({"status": "open", "worker": None})
            else:
                state = str(detail.get("state") or detail.get("status") or "").lower()
                if state in {"claimed", "open", "done"}:
                    current["status"] = state
        elif kind == "dead_end":
            dead_ends.append(
                {
                    "sequence": sequence,
                    "description": str(detail.get("reason") or detail.get("description") or ""),
                }
            )
        elif kind == "flag_found":
            raw_event = raw_events_by_sequence.get(sequence, {})
            flags.append(
                {
                    "sequence": sequence,
                    "flag": str(detail.get("flag") or ""),
                    "verified": bool(event.verified),
                    "evidence_refs": _evidence_refs(detail, raw_event),
                }
            )
        elif kind == "poc_saved":
            poc_id = str(detail.get("poc_id") or f"poc-{sequence}")
            pocs[poc_id] = {
                "poc_id": poc_id,
                "name": str(detail.get("name") or ""),
                "path": str(detail.get("path") or ""),
                "entry_command": str(detail.get("entry_command") or ""),
                "status": str(detail.get("status") or "available"),
                "note": str(detail.get("note") or ""),
                "artifact_id": str(detail.get("artifact_id") or ""),
            }
        elif kind == "poc_concluded":
            poc_id = str(detail.get("poc_id") or "")
            if poc_id in pocs:
                pocs[poc_id]["status"] = str(detail.get("status") or "spent")

    return {
        "available": True,
        "run_id": str(run_id),
        "revision": max((int(event.sequence or 0) for event in events), default=0),
        "facts": facts,
        "key_conditions": key_conditions,
        "intents": list(intents.values()),
        "dead_ends": dead_ends,
        "flags": flags,
        "pocs": list(pocs.values()),
    }


__all__ = ["read_native_graph_snapshot"]
