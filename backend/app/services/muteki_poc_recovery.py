"""Recover an executable PoC from the durable artifacts a Run left behind.

Why this exists: :func:`app.services.muteki_poc_bundle._step_plan` only accepts
an HTTP request that has a persisted ``EvidenceLedger`` reference.  A native
Muteki Worker runs its CLI inside a container and reports the whole turn as one
opaque ``muteki_native_worker`` ToolCall, so that condition is structurally
unreachable — a genuinely solved Run answered ``409
MUTEKI_POC_NOT_REPRODUCIBLE`` ("flag 导出不出来") while the Worker had already
written a working exploit script into its own workspace.

The Run does leave real material behind, so this module recovers from it in
descending order of fidelity:

1. **Worker-authored scripts** — ``muteki/shared/*.sh|py`` etc. The Worker wrote
   these while actually solving, so they are byte-for-byte what ran. Highest
   fidelity; nothing is reconstructed.
2. **The real HTTP requests in the captured CLI transcripts** —
   ``evidence/cli-conversations/*.log``. Turned into a Python ``reproduce.py``
   so the solving process is readable and re-runnable.

Everything here is derived from bytes already on disk. Nothing is invented from
the flag or from prose. Credential-shaped literals are masked, and every output
is labelled **unverified** because this pipeline never executes what it exports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

__all__ = [
    "RecoveredPoc",
    "RecoveredScript",
    "extract_http_requests",
    "recover_poc",
]

# Directories a Worker may have written its own helpers into, most specific
# first.  ``muteki/shared`` is where the native Worker puts hand-written PoCs.
_SCRIPT_DIRS: tuple[str, ...] = (
    "muteki/shared",
    "shared",
    "muteki/.muteki-artifacts",
    "generated",
    "payloads",
    "outputs",
)
_SCRIPT_SUFFIXES = frozenset({".sh", ".py", ".bash"})
# A file whose name says "exploit" is a better PoC candidate than a scratchpad.
_POC_NAME_HINT = re.compile(r"(?i)(poc|exploit|repro|solve|exp[_\-.]|shell|rce)")

_TRANSCRIPT_DIRS: tuple[str, ...] = (
    "muteki/evidence/cli-conversations",
    "evidence/cli-conversations",
)

# Never let a token, password or session cookie leave inside an exported script.
# Three shapes actually occur: ``key=value``, ``key: value``, and the
# ``Authorization: Bearer <token>`` scheme where the secret is one word further
# right than a naive ``key[:=]value`` match would stop.
_SECRET_LITERAL = re.compile(
    r"(?i)"
    r"((?:password|passwd|secret|token|api[_-]?key|authorization|bearer)"
    r"[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)\s+)?)"
    r"([^\s\"';,}\]]+)"
)
_COOKIE_LITERAL = re.compile(r"(?i)((?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)([^\s\"';]+)")
_FLAG_BODY = re.compile(r"(?i)\b(flag|ctf2|dasctf|[a-z0-9_]*ctf[a-z0-9_]*)\{[^{}\r\n]{1,256}\}")

_CURL_FLAGS_WITH_VALUE = frozenset(
    {
        "-X", "--request", "-d", "--data", "--data-raw", "--data-urlencode",
        "--data-binary", "-H", "--header", "--max-time", "-m", "-o", "--output",
        "-D", "--dump-header", "--cookie", "-b", "--cookie-jar", "-c",
        "-A", "--user-agent", "-e", "--referer", "-w", "--write-out", "--url",
        "--connect-timeout", "--retry", "-x", "--proxy", "-u", "--user",
    }
)
_CURL_STOP_TOKENS = frozenset({"|", "&", ";", "&&", "||", ">", ">>", "<"})


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    quoted: bool


@dataclass(frozen=True, slots=True)
class RecoveredScript:
    """One runnable artifact and the durable source it came from."""

    filename: str
    content: str
    language: str
    provenance: str
    source_path: str
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveredPoc:
    """What could be recovered for one Run.

    ``primary`` is the Worker's own script when one exists; ``derived`` is the
    Python transcription of the captured request chain.  At least one is set.
    """

    primary: RecoveredScript | None
    derived: RecoveredScript | None
    scanned_sources: tuple[dict[str, Any], ...] = ()

    @property
    def best(self) -> RecoveredScript:
        assert self.primary or self.derived, "RecoveredPoc requires at least one script"
        return self.primary or self.derived  # type: ignore[return-value]


# ── sanitisation ─────────────────────────────────────────────────────────────


def _mask_secrets(text: str) -> str:
    """Blank credential-shaped literals without disturbing the code around them."""

    masked = _SECRET_LITERAL.sub(lambda m: f"{m.group(1)}{{{{secret_value}}}}", text)
    masked = _COOKIE_LITERAL.sub(lambda m: f"{m.group(1)}{{{{cookie_value}}}}", masked)
    return masked


# Header names whose *entire* value is a credential.  Masking the value alone is
# not enough: ``Authorization: Bearer sk-…`` split on the colon leaves a bare
# ``Bearer sk-…`` with no key remaining to match against.
_SENSITIVE_HEADER = re.compile(
    r"(?i)^(authorization|proxy-authorization|cookie|x-api-key|api-key)$"
)


def _mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    masked: dict[str, str] = {}
    for name, value in headers.items():
        if _SENSITIVE_HEADER.match(str(name).strip()):
            masked[str(name)] = "{{secret_value}}"
        else:
            masked[str(name)] = _mask_secrets(str(value))
    return masked


def _looks_unresolved(value: str) -> bool:
    """Was this argument still a shell variable when the Worker ran it?

    ``$payload`` / ``$(…)`` / backticks cannot be replayed from a transcript, so
    they are surfaced as unresolved instead of being silently dropped or, worse,
    guessed at.
    """

    return any(marker in value for marker in ("$", "`")) or "{{" in value


# ── worker-authored scripts ──────────────────────────────────────────────────


def _worker_scripts(root: Path) -> tuple[list[tuple[Path, str]], list[dict[str, Any]]]:
    """Collect scripts the Worker wrote itself, best candidate first."""

    found: list[tuple[int, str, Path]] = []
    scanned: list[dict[str, Any]] = []
    for relative in _SCRIPT_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        count = 0
        for path in sorted(directory.rglob("*")):
            # ``.muteki_accounts`` holds the CLI engine's own state, never a PoC.
            if not path.is_file() or ".muteki_accounts" in path.parts:
                continue
            if path.suffix.lower() not in _SCRIPT_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            count += 1
            # Rank: a PoC-sounding name first, then the more focused (smaller)
            # file, since scratchpads grow large and PoCs stay short.
            hint = 0 if _POC_NAME_HINT.search(path.name) else 1
            found.append((hint, f"{size:012d}", path))
        if count:
            scanned.append({"path": relative, "kind": "worker_script", "files": count})
    found.sort(key=lambda item: (item[0], item[1], str(item[2])))
    return [(path, _script_dir_for(root, path)) for _hint, _size, path in found], scanned


def _script_dir_for(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


# ── transcript parsing ───────────────────────────────────────────────────────


def _tokenize(text: str) -> list[_Token]:
    """Quote-aware shell split that keeps *whether* a token was quoted.

    Quoting matters because the Worker wraps its real command in
    ``bash -lc "curl … 'http://…'"``; the inner command only becomes visible
    after descending into the quoted token.
    """

    tokens: list[_Token] = []
    buffer: list[str] = []
    quoted = False
    quote: str | None = None

    def flush() -> None:
        nonlocal buffer, quoted
        if buffer:
            tokens.append(_Token("".join(buffer), quoted))
            buffer = []
            quoted = False

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is not None:
            if char == "\\" and quote == '"' and index + 1 < length:
                buffer.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
                index += 1
                continue
            buffer.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            quoted = True
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            buffer.append(text[index + 1])
            index += 2
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        if char in "|&;":
            flush()
            tokens.append(_Token(char, False))
            index += 1
            continue
        buffer.append(char)
        index += 1
    flush()
    return tokens


def _parse_curl_arguments(tokens: list[_Token], start: int) -> tuple[dict[str, Any] | None, int]:
    """Read one curl invocation's arguments, stopping at a shell operator."""

    method: str | None = None
    url = ""
    headers: dict[str, str] = {}
    data: list[tuple[str, str]] = []
    force_get = False
    index = start
    while index < len(tokens):
        token = tokens[index]
        if not token.quoted and token.value in _CURL_STOP_TOKENS:
            break
        value = token.value
        if value in ("--get", "-G"):
            force_get = True
            index += 1
            continue
        if value.startswith("--") and "=" in value:
            flag, inline = value.split("=", 1)
            if flag in _CURL_FLAGS_WITH_VALUE:
                index += 1
                if flag in ("-X", "--request"):
                    method = inline.upper()
                elif flag in ("-d", "--data", "--data-raw", "--data-urlencode", "--data-binary"):
                    data.append(_split_data(inline))
                elif flag in ("-H", "--header"):
                    _store_header(headers, inline)
                continue
        if value.startswith("-") and value in _CURL_FLAGS_WITH_VALUE:
            if index + 1 < len(tokens):
                inline = tokens[index + 1].value
                if value in ("-X", "--request"):
                    method = inline.upper()
                elif value in ("-d", "--data", "--data-raw", "--data-urlencode", "--data-binary"):
                    data.append(_split_data(inline))
                elif value in ("-H", "--header"):
                    _store_header(headers, inline)
            index += 2
            continue
        if value.startswith("-"):
            # A value-less flag (-s, -i, -L, …): nothing to record.
            index += 1
            continue
        if not url:
            url = value
            index += 1
            continue
        index += 1

    if not url:
        return None, index
    if method is None:
        method = "GET" if (force_get or not data) else "POST"
    return {"method": method, "url": url, "headers": headers, "data": data}, index


def _split_data(raw: str) -> tuple[str, str]:
    if "=" in raw:
        key, _, value = raw.partition("=")
        return key, value
    return raw, ""


def _store_header(headers: dict[str, str], raw: str) -> None:
    if ":" in raw:
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()


def _collect_curl_calls(command: str, depth: int = 0) -> list[dict[str, Any]]:
    """Find every curl invocation inside one (possibly nested) shell command."""

    if depth > 4:
        return []
    calls: list[dict[str, Any]] = []
    tokens = _tokenize(command)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        # ``bash -lc "<real command>"``: descend into the quoted payload.
        if token.quoted and "curl" in token.value and depth < 4:
            calls.extend(_collect_curl_calls(token.value, depth + 1))
            index += 1
            continue
        if token.value == "curl":
            call, index = _parse_curl_arguments(tokens, index + 1)
            if call is not None:
                calls.append(call)
            continue
        index += 1
    return calls


def _command_pairs(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(command, output)`` for every executed command in a transcript.

    Two engine shapes are handled: the codex stream
    (``{"item": {"type": "command_execution", …}}``) and the claude stream
    (``{"message": {"content": [{"type": "tool_use", …}]}}``).
    """

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    tool_results: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue

        item = record.get("item")
        if isinstance(item, Mapping) and item.get("type") == "command_execution":
            yield str(item.get("command") or ""), str(item.get("aggregated_output") or "")
            continue

        message = record.get("message")
        if not isinstance(message, Mapping):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use" and str(block.get("name") or "") in {"Bash", "bash"}:
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str) and command:
                    tool_results[command] = str(block.get("id") or "")
                    yield command, ""
            elif block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(part.get("text") or "") for part in content if isinstance(part, Mapping)
                    )
                if content is not None:
                    yield "", str(content)


def extract_http_requests(workspace: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(requests, scanned)`` parsed from the captured CLI transcripts.

    Requests are de-duplicated in first-seen order (the streams repeat a command
    on ``item.started`` and ``item.completed``) and restricted to the challenge
    origin so the exported script cannot be pointed at an unrelated host.
    """

    root = Path(workspace)
    directory, scanned_dirs = _transcript_dirs(root)
    if directory is None:
        return [], []
    files = sorted(directory.rglob("*.log"))
    if not files:
        return [], []

    origin = ""
    requests: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        for command, _output in _command_pairs(path):
            if "curl" not in command:
                continue
            for call in _collect_curl_calls(command):
                url = str(call.get("url") or "")
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"}:
                    continue
                if not origin:
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                elif f"{parsed.scheme}://{parsed.netloc}" != origin:
                    continue
                signature = (
                    str(call.get("method")),
                    url,
                    json.dumps(call.get("data") or [], ensure_ascii=False, sort_keys=True),
                )
                if signature in seen:
                    continue
                seen.add(signature)
                requests.append(
                    {
                        "method": call.get("method"),
                        "url": url,
                        "headers": _mask_headers(dict(call.get("headers") or {})),
                        "data": [list(pair) for pair in call.get("data") or []],
                        "raw_command": _mask_secrets(command)[:2000],
                        "source_path": _relative(root, path),
                    }
                )
    scanned = [
        {"path": _relative(root, directory), "kind": "cli_transcript", "files": len(files)}
    ]
    return requests, scanned


def _transcript_dirs(root: Path) -> tuple[Path | None, list[str]]:
    """Locate the captured transcripts under either workspace layout."""

    for relative in _TRANSCRIPT_DIRS:
        directory = root / relative
        if directory.is_dir():
            return directory, [relative]
    return None, []


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ── script rendering ─────────────────────────────────────────────────────────


def _python_from_requests(
    requests: list[dict[str, Any]],
    *,
    origin: str,
    flag_pattern: str,
    provenance: str,
) -> tuple[str, tuple[str, ...]]:
    """Render the captured request chain as a readable, re-runnable script."""

    steps: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for order, request in enumerate(requests, 1):
        url = str(request["url"])
        path = url[len(origin):] if origin and url.startswith(origin) else url
        params: list[list[str]] = []
        body: list[list[str]] = []
        step_unresolved: list[str] = []
        # A path that still holds ``$NAME`` / ``${sha:0:2}`` was produced by a
        # shell loop or a captured variable; replaying it verbatim would fetch
        # a literal "$NAME". Flag it so the exported script skips it loudly.
        if _looks_unresolved(path):
            step_unresolved.append(f"path={path}")
        for key, value in request.get("data") or []:
            target = params if str(request["method"]).upper() == "GET" else body
            if _looks_unresolved(str(value)):
                step_unresolved.append(f"{key}={value}")
                target.append([str(key), f"〈未解析：{value}〉"])
            else:
                target.append([str(key), str(value)])
        for item in step_unresolved:
            unresolved.append(f"第 {order} 步：{item}")
        steps.append(
            {
                "order": order,
                "method": str(request["method"]),
                "path": path or "/",
                "query": params,
                "body": body,
                "headers": _mask_headers(dict(request.get("headers") or {})),
                "unresolved": step_unresolved,
                "raw_command": request.get("raw_command") or "",
            }
        )

    script = _PYTHON_TEMPLATE.format(
        provenance=provenance,
        flag_pattern=flag_pattern or r"flag\{[^}]+\}",
        origin=origin,
        steps=json.dumps(steps, ensure_ascii=False, indent=2),
    )
    return script, tuple(unresolved)


_PYTHON_TEMPLATE = '''#!/usr/bin/env python3
"""从 Muteki Worker 的 CLI 对话原文恢复出的 PoC 脚本。

来源：{provenance}

⚠️ 本管线未自动执行验证。以下请求是 Worker 解题时真实发出过的，逐条从对话原文转录而来；
本包没有重新执行过它们，因此包内没有任何"已复现确认"的结论。

若某个请求的参数在捕获时仍是 shell 变量，会在步骤中标记为「未解析」，
脚本会跳过该步骤，需人工补齐后才能完整跑通。
每一步都保留了原始 shell 命令作为注释，转录过程不丢信息。

用法：
    export TARGET_URL='{origin}'
    python reproduce.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TARGET_URL = os.environ.get("TARGET_URL", "").strip().rstrip("/")
FLAG_PATTERN = re.compile(os.environ.get("FLAG_PATTERN", r"{flag_pattern}"), re.I)

# 每步字段：顺序、方法、路径、查询参数、请求体、请求头、未解析项、原始命令
STEPS = {steps}


def build_url(step):
    """把路径与查询参数拼成完整 URL。"""
    url = TARGET_URL + step["path"]
    query = [(key, value) for key, value in step["query"]]
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    return url


def main():
    if not TARGET_URL:
        print("缺少 TARGET_URL，例如："
              "TARGET_URL='{origin}' python reproduce.py")
        return 2

    print("提示：以下为对话转录重放，不是本管线执行过的验证。")
    results = []
    for step in STEPS:
        order = step["order"]
        if step["unresolved"]:
            print(f"[{{order}}] 已跳过（参数未解析）：{{step['unresolved']}}")
            print(f"       原始命令：{{step['raw_command'][:300]}}")
            results.append({{"order": order, "ok": False, "status": None,
                           "skipped": "参数未解析"}})
            continue

        url = build_url(step)
        method = step["method"]
        data = None
        headers = dict(step["headers"])
        if step["body"]:
            data = urllib.parse.urlencode(step["body"]).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw, status = response.read(), int(response.status)
        except urllib.error.HTTPError as error:
            raw, status = error.read(), int(error.code)
        except Exception as error:  # noqa: BLE001 - 记录异常，不中断整体重放
            print(f"[{{order}}] {{method}} {{url}} -> 异常 {{type(error).__name__}}")
            results.append({{"order": order, "ok": False, "status": None}})
            continue

        text = raw.decode("utf-8", errors="replace")
        flags = list(dict.fromkeys(FLAG_PATTERN.findall(text)))
        print(f"[{{order}}] {{method}} {{url}} -> HTTP {{status}}（{{len(raw)}} 字节）")
        for flag in flags:
            print(f"       命中 Flag：{{flag}}")
        results.append({{"order": order, "ok": status < 400, "status": status,
                       "flag_matches": flags[:10]}})

    print(json.dumps({{"ok": all(item.get("ok") for item in results),
                      "steps": results}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ── entry point ──────────────────────────────────────────────────────────────


def recover_poc(
    workspace: str | Path,
    *,
    target_url: str = "",
    flag_pattern: str = "",
) -> RecoveredPoc | None:
    """Recover the best available PoC for one Run, or ``None`` if nothing exists."""

    root = Path(workspace)
    if not root.is_dir():
        return None

    scanned: list[dict[str, Any]] = []

    primary: RecoveredScript | None = None
    candidates, script_scan = _worker_scripts(root)
    scanned.extend(script_scan)
    if candidates:
        path, relative = candidates[0]
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        if body.strip():
            primary = RecoveredScript(
                filename=path.name,
                content=_mask_secrets(body),
                language="python" if path.suffix.lower() == ".py" else "bash",
                provenance="Worker 自写脚本（逐字节原样，解题时本人所写）",
                source_path=relative,
            )

    derived: RecoveredScript | None = None
    requests, transcript_scan = extract_http_requests(root)
    scanned.extend(transcript_scan)
    if requests:
        origin = ""
        parsed = urlsplit(str(requests[0]["url"]))
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        if not origin and target_url:
            parsed_target = urlsplit(target_url)
            origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
        content, unresolved = _python_from_requests(
            requests,
            origin=origin,
            flag_pattern=flag_pattern,
            provenance=str(requests[0].get("source_path") or _TRANSCRIPT_DIRS[0]),
        )
        derived = RecoveredScript(
            filename="reproduce.py",
            content=content,
            language="python",
            provenance=f"由 {len(requests)} 条捕获到的 Worker 请求转录而成",
            source_path=str(requests[0].get("source_path") or _TRANSCRIPT_DIRS[0]),
            unresolved=unresolved,
        )

    if primary is None and derived is None:
        return None
    return RecoveredPoc(primary=primary, derived=derived, scanned_sources=tuple(scanned))
