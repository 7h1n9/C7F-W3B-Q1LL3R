import asyncio
import hashlib
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for

PCAP_EXTENSIONS = {".pcap", ".pcapng", ".cap"}
PCAP_MAGICS = {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d", b"\x0a\x0d\x0d\x0a"}
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,79}$")


def _pcap_path(request: JobRequest) -> Path:
    workspace = workspace_for(request.run_id)
    path = safe_child(workspace, str(request.arguments.get("path", "")), "attachments")
    if not path.is_file() or path.suffix.lower() not in PCAP_EXTENSIONS or path.read_bytes()[:4] not in PCAP_MAGICS:
        raise HTTPException(422, detail="path must be a valid PCAP/PCAPNG file below attachments/")
    return path


async def _run(command: list[str]) -> tuple[str, bool]:
    executable = command[0]
    if not shutil.which(executable):
        raise HTTPException(503, detail=f"{executable} is unavailable on the Runner")
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=settings.pcap_timeout_seconds)
    except TimeoutError:
        process.kill(); await process.wait()
        raise HTTPException(408, detail="PCAP command timed out")
    except asyncio.CancelledError:
        process.kill(); await process.wait(); raise
    capped = output[:settings.max_output_bytes]
    if process.returncode != 0:
        raise HTTPException(422, detail=capped.decode(errors="replace")[:1000])
    return capped.decode(errors="replace"), len(output) > len(capped)


def _hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""): hasher.update(chunk)
    return hasher.hexdigest()


async def pcap_metadata(request: JobRequest) -> dict:
    path = _pcap_path(request)
    output, truncated = await _run(["capinfos", "-c", "-s", "-u", "-y", "-E", str(path)])
    values = {}
    for line in output.splitlines():
        if ":" in line:
            key, value = line.split(":", 1); values[key.strip().lower()] = value.strip()
    return {"packet_count": values.get("number of packets"), "file_size": path.stat().st_size, "capture_duration": values.get("data size") or values.get("capture duration"), "start_time": values.get("first packet time"), "end_time": values.get("last packet time"), "encapsulation": values.get("file encapsulation"), "sha256": _hash(path), "raw": output, "truncated": truncated, "summary": f"PCAP metadata for {path.name}"}


async def pcap_protocols(request: JobRequest) -> dict:
    path = _pcap_path(request)
    output, truncated = await _run(["tshark", "-n", "-r", str(path), "-q", "-z", "io,phs"])
    return {"protocol_hierarchy": output, "truncated": truncated, "summary": f"Protocol hierarchy for {path.name}"}


async def pcap_query(request: JobRequest) -> dict:
    path = _pcap_path(request)
    display_filter = str(request.arguments.get("display_filter", "")).strip()
    fields = request.arguments.get("fields", [])
    limit = request.arguments.get("limit", 200)
    if len(display_filter) > 300 or "\n" in display_filter or "\r" in display_filter:
        raise HTTPException(422, detail="display_filter is invalid or too long")
    if not isinstance(fields, list) or not fields or len(fields) > settings.pcap_max_fields or not all(isinstance(field, str) and FIELD_RE.fullmatch(field) for field in fields):
        raise HTTPException(422, detail="fields must be a short list of safe tshark field names")
    if not isinstance(limit, int) or limit < 1 or limit > settings.pcap_max_limit:
        raise HTTPException(422, detail="limit is outside the permitted range")
    command = ["tshark", "-n", "-r", str(path), "-T", "fields", "-E", "separator=\t", "-c", str(limit)]
    if display_filter: command += ["-Y", display_filter]
    for field in fields: command += ["-e", field]
    output, truncated = await _run(command)
    rows = [dict(zip(fields, row.split("\t"), strict=False)) for row in output.splitlines()]
    return {"fields": fields, "display_filter": display_filter, "rows": rows, "truncated": truncated, "summary": f"PCAP query returned {len(rows)} rows"}
