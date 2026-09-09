from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.muteki_poc_bundle import PocBundleUnavailable, build_poc_bundle


class _SessionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "poc_session=ok; Path=/")
            body = b"session established"
        elif self.path == "/verify" and "poc_session=ok" in self.headers.get("Cookie", ""):
            self.send_response(200)
            body = b"flag{runtime-replay-proof}"
        else:
            self.send_response(403)
            body = b"missing session"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _steps() -> list[dict[str, object]]:
    return [
        {
            "order": 1,
            "title_zh": "建立会话",
            "purpose_zh": "获取验证所需的会话 Cookie",
            "tool_name": "http_session_request",
            "normalized_arguments": {"method": "GET", "url": "{{target_url}}/set-cookie"},
            "expected_status": 200,
            "source_tool_call_ids": ["call-1"],
        },
        {
            "order": 2,
            "title_zh": "验证结果",
            "purpose_zh": "复现已验证的受保护结果",
            "tool_name": "http_session_request",
            "normalized_arguments": {"method": "GET", "url": "{{target_url}}/verify"},
            "expected_status": 200,
            "source_tool_call_ids": ["call-2"],
        },
    ]


def test_poc_bundle_is_evidence_backed_and_contains_reproducible_files() -> None:
    bundle = build_poc_bundle(
        run_id="run-poc",
        challenge_name="会话验证题",
        steps=_steps(),
        evidence_by_tool_call={"call-1": ["ev-1"], "call-2": ["ev-2"]},
        evidence_by_artifact={},
    )

    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert names == {
            "README.zh-CN.md",
            "reproduce.py",
            "request-plan.json",
            "evidence-manifest.json",
            "requirements.txt",
        }
        plan = json.loads(archive.read("request-plan.json"))
        script = archive.read("reproduce.py").decode("utf-8")
        assert len(plan) == 2
        assert "ev-1" in archive.read("evidence-manifest.json").decode("utf-8")
        assert "flag{runtime-replay-proof}" not in archive.read("README.zh-CN.md").decode("utf-8")
        compile(script, "reproduce.py", "exec")


def test_poc_bundle_script_replays_cookie_session_and_reports_digest(tmp_path) -> None:
    bundle = build_poc_bundle(
        run_id="run-replay",
        challenge_name="Replay",
        steps=_steps(),
        evidence_by_tool_call={"call-1": ["ev-1"], "call-2": ["ev-2"]},
        evidence_by_artifact={},
    )
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        archive.extractall(tmp_path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SessionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env["TARGET_URL"] = f"http://127.0.0.1:{server.server_port}"
        completed = subprocess.run(
            [sys.executable, str(tmp_path / "reproduce.py")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert [item["status"] for item in payload["steps"]] == [200, 200]
    assert payload["steps"][1]["flag_matches"] == ["flag{runtime-replay-proof}"]
    assert "missing session" not in completed.stdout


def test_poc_bundle_refuses_unlinked_request() -> None:
    with pytest.raises(PocBundleUnavailable, match="MUTEKI_POC_NOT_REPRODUCIBLE"):
        build_poc_bundle(
            run_id="run-no-evidence",
            challenge_name="No evidence",
            steps=_steps(),
            evidence_by_tool_call={},
            evidence_by_artifact={},
        )
