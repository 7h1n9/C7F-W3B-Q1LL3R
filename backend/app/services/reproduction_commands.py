"""Human-copyable command rendering; never exposes Runner Gateway syntax."""

import json
import re
import shlex
from typing import Any


def _safe(value: Any, key: str = "") -> Any:
    if re.search(r"(?i)(cookie|token|password|secret|authorization|api[_-]?key)", key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v, key) for v in value[:20]]
    if isinstance(value, str):
        return re.sub(r"flag\{[^}\r\n]+\}", "flag{<redacted>}", value, flags=re.I)[:2000]
    return value


class ReproductionCommandRenderer:
    def render(self, tool_name: str, arguments: dict) -> str:
        args = _safe(arguments)
        if tool_name in {"http_request", "http_session_request"}:
            url = str(args.get("url") or "{{target_url}}")
            query = args.get("query") if isinstance(args.get("query"), dict) else {}
            if query:
                sep = "&" if "?" in url else "?"
                url += sep + "&".join(f"{k}={v}" for k, v in query.items())
            parts = ["curl", "-i"]
            if tool_name == "http_session_request": parts += ["-c", "cookies.txt", "-b", "cookies.txt"]
            parts += ["-X", str(args.get("method") or "GET").upper()]
            for key, value in (args.get("headers") or {}).items():
                if str(key).lower() not in {"cookie", "authorization"}: parts += ["-H", f"{key}: {value}"]
            body = args.get("body") or args.get("form") or args.get("json")
            if body is not None: parts += ["--data", json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)]
            parts.append(url)
            return shlex.join(parts)
        if tool_name in {"sqlmap_detect", "sqlmap_run"}:
            command = ["sqlmap", "-r", str(args.get("request_file") or "requests/request.txt"), "-p", str(args.get("parameter") or "q"), "--batch"]
            action = args.get("action")
            if tool_name == "sqlmap_run" and action == "dbs": command.append("--dbs")
            elif tool_name == "sqlmap_run" and action == "tables": command += ["--tables", "-D", str(args.get("database") or "")]
            elif tool_name == "sqlmap_run" and action == "columns": command += ["--columns", "-D", str(args.get("database") or ""), "-T", str(args.get("table") or "")]
            elif tool_name == "sqlmap_run" and action == "dump_target": command += ["--dump", "-D", str(args.get("database") or ""), "-T", str(args.get("table") or ""), "-C", ",".join(args.get("columns") or [])]
            if args.get("techniques"): command += ["--technique", "".join(args["techniques"])]
            return shlex.join(command)
        if tool_name in {"script_run", "python_run"}:
            return shlex.join(["python", str(args.get("path") or "scripts/solve.py"), *[str(v) for v in args.get("args", [])]])
        if tool_name == "sandbox_exec":
            return shlex.join([str(args.get("executable") or "<executable>"), *[str(v) for v in args.get("args", [])]])
        if tool_name == "content_discovery":
            return shlex.join(["ffuf", "-u", str(args.get("url") or "{{target_url}}/FUZZ"), "-w", str(args.get("wordlist") or "wordlist.txt")])
        return "# No human renderer for this bounded tool"

    def render_steps(self, steps: list[dict]) -> str:
        return "\n".join(self.render(str(item.get("tool_name")), item.get("normalized_arguments") or item.get("arguments") or {}) for item in steps)


reproduction_command_renderer = ReproductionCommandRenderer()
