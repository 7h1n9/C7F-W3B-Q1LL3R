"""Safe, structured SQLMap execution for authorized challenge workspaces."""

import asyncio
import csv
import hashlib
import json
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for

_ACTIONS = {"detect", "dbs", "tables", "columns", "dump_target", "search_column"}
_TECHNIQUES = {"B", "E", "U", "S", "T", "Q", "BE", "BT", "BU", "BUE", "BEUSTQ"}
_MAX_LEVEL, _MAX_RISK, _MAX_THREADS, _MAX_TIMEOUT = 3, 2, 3, 600


def _workspace_request(request: JobRequest) -> tuple[Path, Path]:
    raw = str(request.arguments.get("request_file") or "")
    if not raw:
        raise HTTPException(422, detail="request_file is required")
    workspace = workspace_for(request.run_id)
    path = safe_child(workspace, raw)
    if path.parent.name != "requests" and not str(path.relative_to(workspace)).replace("\\", "/").startswith("requests/"):
        raise HTTPException(422, detail="request_file must be under requests/")
    if not path.is_file():
        raise HTTPException(404, detail="request_file does not exist")
    return workspace, path


def _request_host(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?im)^Host:\s*([^\s:]+)(?::\d+)?\s*$", text)
    if match:
        return match.group(1).lower()
    match = re.search(r"https?://([^/\s:]+)", text)
    return match.group(1).lower() if match else None


def _validate(request: JobRequest, path: Path) -> dict:
    args = request.arguments
    parameter = str(args.get("parameter") or "")
    action = str(args.get("action") or "")
    if not parameter:
        raise HTTPException(422, detail="parameter is required")
    if action not in _ACTIONS:
        raise HTTPException(422, detail=f"action must be one of {sorted(_ACTIONS)}")
    host = _request_host(path)
    allowed = {str(item).lower() for item in request.allowed_hosts}
    if host and host not in allowed:
        raise HTTPException(403, detail="SQLMap is restricted to the challenge allowlist")
    level = int(args.get("level", 1))
    risk = int(args.get("risk", 1))
    threads = int(args.get("threads", 1))
    timeout = int(args.get("timeout_seconds", 120))
    if not 1 <= level <= _MAX_LEVEL or not 1 <= risk <= _MAX_RISK or not 1 <= threads <= _MAX_THREADS or not 1 <= timeout <= _MAX_TIMEOUT:
        raise HTTPException(422, detail="SQLMap level/risk/threads/timeout exceeds the challenge limits")
    techniques = [str(item).upper() for item in (args.get("techniques") or [])]
    if any(item not in _TECHNIQUES for item in techniques):
        raise HTTPException(422, detail="techniques contains an unsupported SQLMap technique")
    if action in {"tables", "columns", "dump_target"} and not str(args.get("database") or ""):
        raise HTTPException(422, detail=f"{action} requires database")
    if action in {"columns", "dump_target"} and not str(args.get("table") or ""):
        raise HTTPException(422, detail=f"{action} requires table")
    if action == "dump_target" and (not isinstance(args.get("columns"), list) or not args.get("columns") or len(args["columns"]) > 10):
        raise HTTPException(422, detail="dump_target requires one to ten columns")
    return {"parameter": parameter, "action": action, "level": level, "risk": risk, "threads": threads, "timeout": timeout, "techniques": techniques}


def build_sqlmap_argv(request: JobRequest, output_dir: Path) -> tuple[list[str], Path]:
    workspace, request_file = _workspace_request(request)
    normalized = _validate(request, request_file)
    binary = shutil.which("sqlmap") or "sqlmap"
    argv = [binary, "-r", str(request_file), "-p", normalized["parameter"], "--batch", "--disable-coloring", "--output-dir", str(output_dir), "--level", str(normalized["level"]), "--risk", str(normalized["risk"]), "--threads", str(normalized["threads"]), "--timeout", str(normalized["timeout"])]
    if normalized["techniques"]:
        argv += ["--technique", "".join(normalized["techniques"])]
    action = normalized["action"]
    if action == "dbs":
        argv.append("--dbs")
    elif action == "tables":
        argv += ["--tables", "-D", str(request.arguments.get("database") or "")]
    elif action == "columns":
        argv += ["--columns", "-D", str(request.arguments.get("database") or ""), "-T", str(request.arguments.get("table") or "")]
    elif action == "dump_target":
        database, table = str(request.arguments.get("database") or ""), str(request.arguments.get("table") or "")
        columns = request.arguments.get("columns") or []
        if not database or not table or not isinstance(columns, list) or not columns:
            raise HTTPException(422, detail="dump_target requires database, table and columns")
        argv += ["--dump", "-D", database, "-T", table, "-C", ",".join(str(item) for item in columns[:10])]
    elif action == "search_column":
        term = str(request.arguments.get("search_term") or "flag")
        argv += ["--search", "-C", term[:80]]
    return argv, request_file


def _parse_output(raw: str, request: JobRequest, action: str) -> dict:
    lowered = raw.lower()
    injectable = any(term in lowered for term in ("is vulnerable", "injectable", "parameter '"))
    techniques = re.findall(r"Type:\s*([^\n]+)", raw, flags=re.I)
    dbms = None
    match = re.search(r"back-end DBMS:\s*([^\n]+)", raw, flags=re.I)
    if match:
        dbms = match.group(1).strip()
    candidates = sorted(set(re.findall(r"flag\{[^}\r\n]+\}", raw, flags=re.I)))
    return {
        "injectable": injectable,
        "parameter": request.arguments.get("parameter"),
        "technique": techniques[0].strip() if techniques else None,
        "dbms": dbms,
        "databases": re.findall(r"\[\*\]\s+([^\r\n]+)", raw) if action == "dbs" else [],
        "tables": [], "columns": [], "dumped_rows": [],
        "flag_candidates": candidates,
        "command_redacted": "",
        "raw_output_path": "",
    }


def _merge_dump_files(structured: dict, run_dir: Path) -> dict:
    rows: list[dict] = []
    for path in run_dir.rglob("*.csv"):
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                rows.extend(list(csv.DictReader(handle))[:100])
        except OSError:
            continue
    if rows:
        structured["dumped_rows"] = rows
        text = json.dumps(rows, ensure_ascii=False)
        structured["flag_candidates"] = sorted(set(re.findall(r"flag\{[^}\r\n]+\}", text, flags=re.I)))
    return structured


async def sqlmap_run(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id)
    run_dir = workspace / "outputs" / "sqlmap" / hashlib.sha256(json.dumps(request.arguments, sort_keys=True).encode()).hexdigest()[:16]
    run_dir.mkdir(parents=True, exist_ok=True)
    argv, _ = build_sqlmap_argv(request, run_dir)
    raw_path = run_dir / "raw.log"
    result_path = run_dir / "result.json"
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(workspace))
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=min(int(request.arguments.get("timeout_seconds", 120)), _MAX_TIMEOUT) + 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {"status": "FAILED", "error_code": "SQLMAP_TIMEOUT", "summary": "SQLMap timed out"}
    except FileNotFoundError:
        return {"status": "FAILED", "error_code": "TOOL_NOT_INSTALLED", "summary": "sqlmap is not installed"}
    raw = output.decode("utf-8", errors="replace")
    raw_path.write_text(raw, encoding="utf-8")
    normalized = _validate(request, _workspace_request(request)[1])
    structured = _merge_dump_files(_parse_output(raw, request, normalized["action"]), run_dir)
    structured["raw_output_path"] = str(raw_path.relative_to(workspace)).replace("\\", "/")
    structured["command_redacted"] = " ".join(argv).replace(str(workspace), "<workspace>")
    result_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "COMPLETED" if process.returncode == 0 else "FAILED", "summary": "SQLMap completed", "exit_code": process.returncode, "artifact_path": str(result_path.relative_to(workspace)).replace("\\", "/"), "structured_result": structured}


async def sqlmap_detect(request: JobRequest) -> dict:
    args = dict(request.arguments)
    args["action"] = "detect"
    return await sqlmap_run(request.model_copy(update={"arguments": args}))
