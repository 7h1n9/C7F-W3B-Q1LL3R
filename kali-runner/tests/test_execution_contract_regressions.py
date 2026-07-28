from pathlib import Path

import pytest
import httpx
import asyncio

from app.executors.http_executor import execute_http
from app.executors.python_executor import python_run
from app.executors.target_allowlist import target_allowed
from app.models import JobRequest
from app.workspace.paths import initialize_workspace
from app.config import settings
from app.executors.script_executor import script_run


@pytest.mark.asyncio
async def test_python_run_is_offline_and_accepts_local_analysis(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("offline-python")
    (workspace / "scripts" / "analysis.py").write_text("from pathlib import Path\nprint(Path('scripts/analysis.py').exists())\n", encoding="utf-8")
    result = await python_run(JobRequest(run_id="offline-python", tool="python_run", arguments={"path": "scripts/analysis.py"}))
    assert result["exit_code"] == 0
    assert result["output"].strip() == "True"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["import requests\nrequests.get('http://x')", "import socket\nsocket.create_connection(('x', 80))", "import subprocess\nsubprocess.run(['curl', 'x'])"])
async def test_python_run_rejects_network_capable_source(tmp_path: Path, source: str) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("offline-reject")
    (workspace / "scripts" / "bad.py").write_text(source, encoding="utf-8")
    result = await python_run(JobRequest(run_id="offline-reject", tool="python_run", arguments={"path": "scripts/bad.py"}))
    assert result["status"] == "FAILED"
    assert result["error_code"] == "PYTHON_RUN_NETWORK_FORBIDDEN"
    assert result["tool_execution_completed"] is False


def test_allowlist_preserves_private_ip_and_nonstandard_port() -> None:
    assert target_allowed("http://192.168.236.1:28319/api/check", ["192.168.236.1"])
    assert target_allowed("http://localhost:18081/health", ["localhost:18081"])
    assert not target_allowed("http://other.local:28319/api/check", ["192.168.236.1"])


@pytest.mark.asyncio
async def test_http_executor_accepts_authorized_metadata_ip(monkeypatch) -> None:
    async def fake_request(*args, **kwargs):
        raise httpx.ConnectError("lab target unavailable", request=httpx.Request("GET", "http://169.254.169.254/latest"))

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    result = await execute_http(JobRequest(run_id="http", allowed_hosts=["169.254.169.254"], tool="http_request", arguments={"url": "http://169.254.169.254/latest"}))
    assert result["status"] == "FAILED" or result.get("error")


@pytest.mark.asyncio
async def test_target_allowlist_proxy_reaches_authorized_port_and_blocks_other_host(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("proxy-enforcement")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        (workspace / "scripts" / "authorized.py").write_text("from urllib.request import urlopen\nprint(urlopen('http://127.0.0.1:%d/ok').read().decode())\n" % port, encoding="utf-8")
        result = await script_run(JobRequest(run_id="proxy-enforcement", allowed_hosts=[f"127.0.0.1:{port}"], tool="script_run", arguments={"path": "scripts/authorized.py", "interpreter": "python", "network_mode": "target_allowlist"}))
        assert result["exit_code"] == 0
        assert result["output"].strip() == "hello"
        (workspace / "scripts" / "unauthorized.py").write_text("from urllib.request import urlopen\nprint(urlopen('http://127.0.0.1:%d/no').read().decode())\n" % (port + 1), encoding="utf-8")
        blocked = await script_run(JobRequest(run_id="proxy-enforcement", allowed_hosts=[f"127.0.0.1:{port}"], tool="script_run", arguments={"path": "scripts/unauthorized.py", "interpreter": "python", "network_mode": "target_allowlist"}))
        assert blocked["exit_code"] != 0
    finally:
        server.close()
        await server.wait_closed()
