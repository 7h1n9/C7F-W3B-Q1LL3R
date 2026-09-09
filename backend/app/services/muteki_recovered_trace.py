"""Read-only recovery of structured Muteki Worker execution records.

The native solver deliberately keeps its own Blackboard and does not project
every in-container HTTP request into the legacy ORM tables.  This module is a
presentation boundary only: it reads immutable records already stored inside
one Run workspace and materializes a sanitized trace for the board, write-up,
and PoC exporter.  It never calls the target and never writes Solver state.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|session)"
)
_FLAG_PATTERN = re.compile(r"flag\{[^{}\r\n]*\}", re.IGNORECASE)
_HTTP_LINE = re.compile(
    r"^(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+(?P<target>\S+)"
    r"(?:\s+with\s+form\s+(?P<form>.+?))?$",
    re.IGNORECASE,
)


def _safe_text(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "")
    text = _FLAG_PATTERN.sub("{{verified_flag}}", text)
    text = re.sub(
        r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|session)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1={{secret_value}}",
        text,
    )
    return text[:limit]


def _safe_value(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "{{secret_value}}"
    if isinstance(value, Mapping):
        return {str(name): _safe_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item, key=key) for item in value[:50]]
    if isinstance(value, str):
        return _safe_text(value, limit=4000)
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _scoped_path(root: Path, path: Path) -> Path | None:
    try:
        resolved = path.resolve()
        if root not in resolved.parents or ".muteki_accounts" in resolved.parts:
            return None
        return resolved if resolved.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _relative_target(value: Any, target_url: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    target = urlsplit(target_url)
    if parsed.scheme and parsed.netloc:
        if target.netloc and parsed.netloc != target.netloc:
            return ""
        return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    if raw.startswith("/"):
        return raw
    return ""


def _request_arguments(method: str, target: str, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"method": method.upper(), "url": target}
    if form:
        arguments["form"] = {str(key): _safe_value(value, key=str(key)) for key, value in form.items()}
    return arguments


def _parse_form(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split("&"):
        if "=" in item:
            key, raw = item.split("=", 1)
            result[key.strip()] = raw.strip()
    return result


def _summary_zh(content: str) -> str:
    value = _safe_text(content, limit=500)
    lower = value.casefold()
    if "department=all" in lower and "audit/events" in lower:
        return "未授权的审计查询在 department=all 时返回了内部事件记录。"
    if "audit/export" in lower or "export" in lower and "event_id" in lower:
        return "使用已发现的事件编号调用导出接口，接口生成了报告地址。"
    if "/reports/" in lower or "returned the flag" in lower:
        return "访问导出的报告地址后得到最终验证结果。"
    if lower.startswith("get ") or lower.startswith("post "):
        return f"已确认请求链步骤：{value}"
    return value if re.search(r"[\u4e00-\u9fff]", value) else f"已确认事实：{value}"


def _evidence_ref(file_digest: str, index: int) -> str:
    return f"recovered:{file_digest[:16]}:{index}"


def _source_files(root: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    patterns = (
        (root / "muteki" / ".muteki-artifacts", "worker_http"),
        (root / "muteki" / "shared", "worker_trace"),
        (root / "shared", "worker_trace"),
        (root / "evidence" / "native-workers", "worker_output"),
    )
    seen: set[Path] = set()
    for directory, kind in patterns:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path in seen:
                continue
            if path.name == "http-evidence.json" or (
                path.suffix.lower() == ".txt"
                and ("audit" in path.name.lower() or kind == "worker_output")
            ):
                scoped = _scoped_path(root, path)
                if scoped is not None:
                    seen.add(scoped)
                    candidates.append((scoped, kind))
    # Prefer the non-sanitized shared trace when both copies exist.  The
    # output is sanitized before it leaves this module in either case.
    candidates.sort(key=lambda item: (".muteki_sanitized_" in item[0].name, str(item[0])))
    return candidates


def _http_records(path: Path, target_url: str, file_digest: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], []
    records = payload.get("records") if isinstance(payload, Mapping) else None
    if not isinstance(records, list):
        return [], []
    steps: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            continue
        method = str(record.get("method") or "GET").upper()
        url = _relative_target(record.get("url"), target_url)
        if not url:
            continue
        ref = _evidence_ref(file_digest, index)
        status = record.get("status_code")
        body = record.get("body")
        body_summary = _safe_text(body, limit=700) if body else ""
        evidence.append(
            {
                "id": ref,
                "evidence_type": "WORKER_HTTP_EXECUTION_RECORD",
                "status": "OBSERVED",
                "summary": f"{method} {url} -> HTTP {status}; {body_summary}"[:1000],
                "source_path": path.relative_to(path.parents[3]).as_posix()
                if len(path.parents) > 3
                else path.name,
                "sha256": file_digest,
            }
        )
        steps.append(
            {
                "order": index,
                "title_zh": "执行过的 HTTP 请求",
                "purpose_zh": "补充 Worker 已完成的侦察记录，不作为最终漏洞结论。",
                "tool_name": "http_session_request" if record.get("tool") == "http_session_request" else "http_request",
                "normalized_arguments": _request_arguments(method, url),
                "expected_status": int(status) if isinstance(status, int) else None,
                "expected_evidence": [f"HTTP 状态码为 {status}"],
                "evidence_refs": [ref],
                "source_artifact_ids": [ref],
                "response_summary": body_summary,
                "verified": False,
                "source_path": path.name,
            }
        )
    return steps, evidence


def _trace_text(path: Path, target_url: str, file_digest: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], [], []
    facts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    pocs: list[dict[str, Any]] = []
    lines = text.splitlines()
    for line_number, line in enumerate(lines, 1):
        marker = re.search(r"VERIFIED_FACT\s*=\s*(.+)$", line, flags=re.IGNORECASE)
        if marker:
            content = _safe_text(marker.group(1), limit=1200)
            ref = _evidence_ref(file_digest, line_number)
            evidence.append(
                {
                    "id": ref,
                    "evidence_type": "WORKER_VERIFIED_EXECUTION_RECORD",
                    "status": "VERIFIED",
                    "summary": content,
                    "source_path": path.name,
                    "sha256": file_digest,
                }
            )
            facts.append(
                {
                    "content": content,
                    "summary_zh": _summary_zh(content),
                    "verified": True,
                    "evidence_refs": [ref],
                    "source_worker_id": "recovered-worker-record",
                }
            )
        poc_marker = re.search(r"POC_SAVE\s*=\s*([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)", line)
        if poc_marker:
            pocs.append(
                {
                    "poc_id": f"recovered-{file_digest[:12]}",
                    "name": "Worker execution record",
                    "path": poc_marker.group(1).strip(),
                    "entry_command": _safe_text(poc_marker.group(2).strip(), limit=600),
                    "status": poc_marker.group(3).strip(),
                    "note": _safe_text(poc_marker.group(4).strip(), limit=600),
                    "artifact_id": _evidence_ref(file_digest, line_number),
                }
            )
        before_arrow = line.split("->", 1)[0].strip()
        match = _HTTP_LINE.match(before_arrow)
        if not match:
            continue
        method = match.group("method").upper()
        url = _relative_target(match.group("target"), target_url)
        if not url:
            continue
        form = _parse_form(match.group("form") or "")
        response_line = line
        status_match = re.search(r"->\s*(\d{3})", line)
        if status_match is None:
            for continuation in lines[line_number:line_number + 3]:
                continuation_match = re.search(r"->\s*(\d{3})", continuation)
                if continuation_match is not None:
                    status_match = continuation_match
                    response_line = f"{line} {continuation.strip()}"
                    break
        status = int(status_match.group(1)) if status_match else None
        ref = _evidence_ref(file_digest, line_number)
        evidence.append(
            {
                "id": ref,
                "evidence_type": "WORKER_REPLAYABLE_HTTP_RECORD",
                "status": "VERIFIED",
                "summary": _safe_text(response_line, limit=1000),
                "source_path": path.name,
                "sha256": file_digest,
            }
        )
        arguments = _request_arguments(method, url, form)
        steps.append(
            {
                "order": len(steps) + 1,
                "title_zh": "复现已确认的 HTTP 请求",
                "purpose_zh": _summary_zh(line),
                "tool_name": "http_session_request" if method in {"POST", "PUT", "PATCH"} else "http_request",
                "normalized_arguments": arguments,
                "expected_status": status,
                "expected_evidence": [_summary_zh(response_line)],
                "evidence_refs": [ref],
                "source_artifact_ids": [ref],
                "response_summary": _safe_text(response_line, limit=700),
                "verified": True,
                "source_path": path.name,
            }
        )
    return steps, facts, evidence, pocs


def load_recovered_trace(*, workspace: str | Path, target_url: str = "") -> dict[str, Any]:
    """Load a sanitized trace from one Run workspace without any writes."""

    root = Path(workspace).resolve()
    if not root.is_dir():
        return {"available": False, "steps": [], "replayable_steps": [], "facts": [], "evidence": [], "pocs": []}
    all_steps: list[dict[str, Any]] = []
    replayable_steps: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    pocs: list[dict[str, Any]] = []
    source_files: list[dict[str, Any]] = []
    seen_step_keys: set[str] = set()
    seen_evidence: set[str] = set()
    for path, kind in _source_files(root):
        try:
            file_digest = _digest(path)
        except OSError:
            continue
        source_files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "kind": kind,
                "sha256": file_digest,
            }
        )
        if path.name == "http-evidence.json":
            steps, rows = _http_records(path, target_url, file_digest)
            for row in rows:
                if row["id"] not in seen_evidence:
                    evidence.append(row)
                    seen_evidence.add(row["id"])
            for step in steps:
                key = json.dumps(step["normalized_arguments"], sort_keys=True, ensure_ascii=False)
                if key not in seen_step_keys:
                    all_steps.append(step)
                    seen_step_keys.add(key)
            continue
        steps, new_facts, rows, new_pocs = _trace_text(path, target_url, file_digest)
        for row in rows:
            if row["id"] not in seen_evidence:
                evidence.append(row)
                seen_evidence.add(row["id"])
        facts.extend(new_facts)
        pocs.extend(new_pocs)
        for step in steps:
            key = json.dumps(step["normalized_arguments"], sort_keys=True, ensure_ascii=False)
            if key in seen_step_keys:
                continue
            replayable_steps.append(step)
            all_steps.append(step)
            seen_step_keys.add(key)
    # A saved export URL is usually single-use/dynamic.  Make the final GET
    # depend on the preceding JSON export response instead of hard-coding it.
    for index, step in enumerate(replayable_steps):
        arguments = step.get("normalized_arguments")
        if not isinstance(arguments, dict):
            continue
        url = str(arguments.get("url") or "")
        if "/reports/" in url and index > 0:
            arguments["url"] = "{{previous:report_url}}"
        if str(arguments.get("url") or "") == "/api/audit/export":
            arguments["extract_json_path"] = {"save_as": "report_url", "path": "download_url"}
    return {
        "available": bool(source_files),
        "source_files": source_files,
        "steps": all_steps,
        "replayable_steps": replayable_steps,
        "facts": facts,
        "evidence": evidence,
        "pocs": pocs,
    }


def merge_recovered_state(state: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    """Add recovered presentation data without mutating the native snapshot."""

    result = dict(state)
    if not trace.get("available"):
        return result
    facts = [dict(item) for item in result.get("facts", []) if isinstance(item, Mapping)]
    key_conditions = [dict(item) for item in result.get("key_conditions", []) if isinstance(item, Mapping)]
    base_sequence = max((int(item.get("sequence") or 0) for item in facts), default=0)
    known_refs = {str(ref) for item in facts for ref in item.get("evidence_refs", []) or []}
    for offset, fact in enumerate(trace.get("facts", []) or [], 1):
        refs = [str(ref) for ref in fact.get("evidence_refs", []) or []]
        if not refs or all(ref in known_refs for ref in refs):
            continue
        sequence = base_sequence + offset
        row = {
            "sequence": sequence,
            "content": str(fact.get("content") or ""),
            "summary_zh": str(fact.get("summary_zh") or fact.get("content") or ""),
            "verified": bool(fact.get("verified")),
            "evidence_refs": refs,
            "source_worker_id": str(fact.get("source_worker_id") or "recovered-worker-record"),
            "recovered": True,
        }
        facts.append(row)
        if row["verified"]:
            key_conditions.append(dict(row))
    result["facts"] = facts
    result["key_conditions"] = key_conditions
    result["pocs"] = list(result.get("pocs", []) or []) + list(trace.get("pocs", []) or [])
    result["recovered_trace"] = dict(trace)
    result["recovered_evidence"] = list(trace.get("evidence", []) or [])
    result["recovered"] = True
    return result


__all__ = ["load_recovered_trace", "merge_recovered_state"]
