from __future__ import annotations

import asyncio
import json as json_module
from pathlib import Path
from types import SimpleNamespace

from app.services.muteki_llm_report import (
    _parse_message,
    LLM_REPORT_CACHE_KEY,
    _is_payload_defect,
    _parameter_constraint,
    _post_completion,
    _should_try_next_config,
    build_model_input,
    load_worker_conversations,
    report_fingerprint,
)
from app.services.muteki_writeup import render_muteki_writeup


def _writeup_kwargs(**overrides):
    base = dict(
        challenge=SimpleNamespace(name="Sentinel-1B", target_url="http://target.local"),
        run=SimpleNamespace(id="run-llm", solver_mode="muteki", status="COMPLETED_SOLVED"),
        result=SimpleNamespace(status="COMPLETED_SOLVED", flag="CTF2{proof}"),
        graph_state={
            "revision": 8,
            "facts": [{"sequence": 4, "verified": True, "content": "Flag recovered via beam search", "evidence_refs": ["ev-1"]}],
            "dead_ends": [{"description": "Greedy top-1 hits canaries"}],
            "intents": [],
        },
        calls=[SimpleNamespace(tool_name="muteki_native_worker", status="COMPLETED", arguments_json={"engine": "claude"})],
        observations=[],
        evidence=[SimpleNamespace(id="ev-1", evidence_type="WORKER_OUTPUT", status="VERIFIED", summary="kept", tool_call_id="", artifact_id=None)],
        poc_available=False,
    )
    base.update(overrides)
    return base


def _llm_report(sections: dict | None = None, *, status: str = "completed", **extra) -> dict:
    payload = {
        "status": status,
        "conversation_available": True,
        "conversation_chars": 42_000,
        "usage": {"model": "kimi-k3", "total_tokens": 1_234},
        "sections": sections
        or {
            "one_liner": "对 /predict 做 beam search 反演，避开蜜罐分支。",
            "vuln_type": "MODEL_INVERSION",
            "attack_chain": ["侦察 /predict", "改用 beam search", "提交校验"],
            "execution": [{"step": 1, "title": "取模型元信息", "detail": "GET /model 得到 context_len=4"}],
            "repro": "python3 beam.py http://target",
            "failed_paths": ["贪心 top-1 落入蜜罐"],
            "remediation": ["限制 /predict 查询频率"],
            "notes": "",
        },
    }
    payload.update(extra)
    return payload


def test_llm_sections_replace_the_template_narrative() -> None:
    text = render_muteki_writeup(**_writeup_kwargs(llm_report=_llm_report()))

    assert "对 /predict 做 beam search 反演" in text
    assert "1. 侦察 /predict" in text
    assert "**1. 取模型元信息**" in text
    assert "python3 beam.py http://target" in text
    assert "- 限制 /predict 查询频率" in text
    # Deterministic sections must stay machine-authored, untouched by the model.
    assert "Flag recovered via beam search" in text
    assert "已验证 Flag：CTF2{proof}" in text
    # The old templated line was wrong for every non-SQL challenge.
    assert "实施参数化查询" not in text
    # Reproduced scripts are not claimed to be executed.
    assert "尚未在本管线内自动执行验证" in text


def test_writeup_header_has_no_placeholder_fields() -> None:
    text = render_muteki_writeup(**_writeup_kwargs())

    assert "???" not in text
    assert "- **解题状态：**已解出" in text
    assert "- **语义分析：**未运行（0 项）" in text


def test_short_conversation_is_reported_as_unavailable() -> None:
    text = render_muteki_writeup(**_writeup_kwargs(llm_report=_llm_report(conversation_available=False, conversation_chars=92)))

    assert "无可用 Worker 对话" in text
    assert "未捕获到可用的 Worker 对话" in text


def test_failed_llm_generation_falls_back_to_templates() -> None:
    text = render_muteki_writeup(**_writeup_kwargs(llm_report={"status": "failed", "error_code": "LLM_REPORT_TIMEOUT", "sections": {}}))

    assert "LLM 生成状态：failed" in text
    assert "确定性模板渲染" in text
    # Still a complete document: no dangling "no PoC" claim without the disclaimers.
    assert "已验证 Flag：CTF2{proof}" in text


def test_model_input_redacts_secrets_and_bounds_conversation(tmp_path) -> None:
    conv = tmp_path / "evidence" / "cli-conversations"
    conv.mkdir(parents=True)
    (conv / "20261011T000000-001-claude.log").write_text(
        "POST /login password=hunter2 api_key=abc123\n" + "x" * 5_000,
        encoding="utf-8",
    )

    conversations = load_worker_conversations(tmp_path)
    assert conversations["available"] is True
    assert len(conversations["files"]) == 1

    model_input = build_model_input(
        challenge=SimpleNamespace(name="T", target_url="http://t", category="web", description="d"),
        run=SimpleNamespace(status="COMPLETED_SOLVED", solver_mode="muteki", started_at=None, finished_at=None),
        graph_state={"revision": 1, "facts": [], "dead_ends": [], "intents": []},
        calls=[],
        evidence=[],
        conversations=conversations,
        poc_available=False,
        verified_flag="CTF2{proof}",
    )

    assert model_input["worker_conversation"]["available"] is True
    assert model_input["run"]["tool_call_count"] == 0
    assert report_fingerprint(model_input) != report_fingerprint({**model_input, "revision_hint": 2})


def test_conversation_sink_writes_one_file_per_turn(tmp_path) -> None:
    from muteki.solver.cli_driver import attach_conversation_sink, capture_cli_conversation

    class _Driver:
        name = "claude"

    driver = _Driver()
    attach_conversation_sink(driver, directory=str(tmp_path / "conv"), label="claude/worker-1/intent-7")
    capture_cli_conversation(driver, "turn one: GET /model", "")
    capture_cli_conversation(driver, "turn two: POST /predict", "")

    files = sorted((tmp_path / "conv").glob("*.log"))
    assert len(files) == 2  # every turn is a new file; never overwritten
    assert "claude-worker-1-intent-7" in files[0].name
    assert "POST /predict" in files[1].read_text(encoding="utf-8")

    # No sink → no file, no exception (drivers without a workspace are normal).
    capture_cli_conversation(_Driver(), "orphan", "")
    assert len(sorted((tmp_path / "conv").glob("*.log"))) == 2


def test_conversation_capture_maps_container_workspace_to_host(tmp_path) -> None:
    """The container backend hands the Worker a container path, but the sink is
    written by the HOST process — an un-mapped path would land under the current
    drive root and be invisible to the report writer."""

    from app.solver.muteki.worker.official_worker import (
        _conversation_host_workspace,
        _with_conversation_capture,
    )

    class _Handle:
        host_workspace = str(tmp_path / "data-workspaces" / "run-1")

    handle = _Handle()
    assert _conversation_host_workspace("/home/kali/workspace", handle) == handle.host_workspace
    assert (
        _conversation_host_workspace("/home/kali/workspace/evidence", handle)
        == str(Path(handle.host_workspace) / "evidence")
    )

    class _Driver:
        name = "codex"

    driver = _with_conversation_capture(
        _Driver(),
        workspace="/home/kali/workspace",
        engine="codex",
        worker_id="w1",
        intent_id="i1",
        handle=handle,
    )
    from muteki.solver.cli_driver import capture_cli_conversation

    capture_cli_conversation(driver, "GET /model -> context_len 4", "")
    files = sorted((Path(handle.host_workspace) / "evidence" / "cli-conversations").glob("*.log"))
    assert len(files) == 1
    assert "context_len 4" in files[0].read_text(encoding="utf-8")

    # No handle (bare host subprocess) → the workspace is already a host path.
    assert _conversation_host_workspace(str(tmp_path / "host-ws"), None) == str(tmp_path / "host-ws")


def test_report_json_tolerates_model_escapes_from_transcripts() -> None:
    """A model narrating a raw CLI transcript emits invalid escapes; one bad
    byte must not discard the whole report."""

    from app.services.muteki_llm_report import _parse_message

    # `\d` and `\S` are NOT valid JSON escapes — real output does this when the
    # transcript contains regexes.
    raw = (
        '{"one_liner":"decode the flag","attack_chain":["match \\d+ against /predict"],'
        '"execution":[{"step":1,"title":"probe","detail":"ran \\S+ over the vocab"}],'
        '"repro":"curl -s /model | jq .vocab","failed_paths":[],"remediation":[]}'
    )
    sections = _parse_message(raw)
    assert sections["one_liner"] == "decode the flag"
    assert "match" in sections["attack_chain"][0]
    assert sections["execution"][0]["step"] == 1
    assert "vocab" in sections["repro"]

    # Fenced output still works.
    fenced = "```json\n" + raw + "\n```"
    assert _parse_message(fenced)["one_liner"] == "decode the flag"


def test_llm_cache_is_keyed_on_input_fingerprint(tmp_path) -> None:
    conv = tmp_path / "evidence" / "cli-conversations"
    conv.mkdir(parents=True)
    (conv / "a.log").write_text("hello", encoding="utf-8")

    loader = load_worker_conversations(tmp_path)
    first = build_model_input(
        challenge=SimpleNamespace(name="T", target_url="http://t", category="web", description="d"),
        run=SimpleNamespace(status="COMPLETED_SOLVED", solver_mode="muteki", started_at=None, finished_at=None),
        graph_state={"revision": 1, "facts": [], "dead_ends": [], "intents": []},
        calls=[],
        evidence=[],
        conversations=loader,
        poc_available=False,
        verified_flag="CTF2{proof}",
    )
    moved = dict(first)
    moved["board"] = {**first["board"], "revision": 2}
    # A board change invalidates the cached authoring; identical input does not.
    assert report_fingerprint(first) != report_fingerprint(moved)
    assert LLM_REPORT_CACHE_KEY == "muteki_llm_report"
    assert json_module.dumps(first, ensure_ascii=False, default=str)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _http_status_error(status: int, body: str):
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:8787/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _http_status_error(status: int, body: str):
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:8787/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_every_per_config_failure_advances_to_the_next_candidate() -> None:
    """Only a genuine payload defect may stop the config walk.

    Regression cover for a real outage: a broken relay answered with an HTML
    Cloudflare "524: A timeout occurred" page under HTTP 400.  Its body never
    mentions ``model``, so a "not a model problem → fail fast" rule aborted the
    report while four healthy configs sat unused.
    """

    # Dead model name (the real upstream body).
    dead = _http_status_error(
        400,
        '{"error":{"message":"上游返回 HTTP 400: {\\"code\\":11102,'
        '\\"msg\\":\\"model [kimi-k3-1] service info not found\\"}"}}',
    )
    assert _should_try_next_config(dead) is True

    # A proxy/edge HTML error page is an endpoint failure, not our payload.
    proxy_page = _http_status_error(400, "<html><title>524: A timeout occurred</title></html>")
    assert _should_try_next_config(proxy_page) is True

    # Per-config auth / rate-limit / upstream-5xx problems.
    assert _should_try_next_config(_http_status_error(401, '{"error":{"message":"bad key"}}')) is True
    assert _should_try_next_config(_http_status_error(429, '{"error":{"message":"slow down"}}')) is True
    assert _should_try_next_config(_http_status_error(503, '{"error":{"message":"upstream down"}}')) is True

    # Transport failures are infrastructure, not payload.
    import httpx

    assert _should_try_next_config(httpx.ConnectTimeout("timed out")) is True

    # Model OUTPUT problems are non-deterministic — a retry is the right move.
    assert _should_try_next_config(ValueError("LLM_REPORT_JSON_UNPARSEABLE")) is True
    assert _should_try_next_config(ValueError("LLM_REPORT_NO_SECTIONS")) is True
    assert _should_try_next_config(ValueError("LLM_REPORT_MODEL_UNAVAILABLE")) is True


def test_only_an_explicit_payload_error_stops_the_walk() -> None:
    """An API error naming a field WE send would fail on every config."""

    offending = _http_status_error(
        400, '{"error":{"type":"invalid_request_error","message":"response_format is invalid"}}'
    )
    assert _should_try_next_config(offending) is False
    assert _is_payload_defect(offending) is True

    # An unrelated field name must not be mistaken for our payload's fault.
    other = _http_status_error(400, '{"error":{"message":"unknown field: foo"}}')
    assert _is_payload_defect(other) is False


def test_a_per_model_parameter_limit_is_negotiated_not_fatal() -> None:
    """The Kimi relay rejects ``temperature``; that must not end the walk.

    Measured on 2026-10-20: every candidate config was tried and all five
    report sections came back failed, because the relay answers
    ``400 {"error":{"message":"field temperature invalid, only 1 is allowed for
    this model","type":"invalid_request_error"}}`` — an ``invalid_request_error``
    naming a field we send, which the old rule read as OUR payload defect and
    used to abort.  Yet api.deepseek.com answers the identical body with 200.
    """

    kimi = _http_status_error(
        400,
        '{"error":{"message":"field temperature invalid, only 1 is allowed for '
        'this model","type":"invalid_request_error"}}',
    )
    # Named, with the value the upstream actually demands.
    assert _parameter_constraint(kimi) == ("temperature", 1.0)
    # Not our payload's fault — so the walk continues to the next config.
    assert _is_payload_defect(kimi) is False
    assert _should_try_next_config(kimi) is True

    # A field the upstream refuses without offering a value is dropped instead.
    no_value = _http_status_error(400, '{"error":{"message":"field seed invalid"}}')
    assert _parameter_constraint(no_value) == ("seed", None)

    # A field we do not send is not ours to negotiate.
    foreign = _http_status_error(400, '{"error":{"message":"field user_id invalid"}}')
    assert _parameter_constraint(foreign) is None

    # Only a structural rejection of a universal field stops the walk.
    structural = _http_status_error(400, '{"error":{"message":"messages is required"}}')
    assert _parameter_constraint(structural) is None
    assert _is_payload_defect(structural) is True


def test_negotiated_payload_drops_or_adjusts_the_rejected_field() -> None:
    """Falsifiable: without ``payload_overrides`` the body keeps temperature 0.2."""

    import httpx

    captured: list[dict] = []

    class _Client:
        async def post(self, url, *, headers=None, json=None):  # noqa: A002 - httpx's own name
            captured.append(dict(json or {}))
            request = httpx.Request("POST", url)
            body = json_module.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json_module.dumps(
                                    {
                                        "one_liner": "ok",
                                        "vuln_type": "X",
                                        "attack_chain": [],
                                        "execution": [],
                                        "repro": "r",
                                        "failed_paths": [],
                                        "remediation": [],
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {},
                }
            )
            return httpx.Response(200, text=body, request=request)

    import asyncio

    async def run() -> None:
        await _post_completion(
            _Client(),
            base_url="http://127.0.0.1:8787/v1",
            model="kimi-k3",
            api_key="k",
            model_input={},
        )
        await _post_completion(
            _Client(),
            base_url="http://127.0.0.1:8787/v1",
            model="kimi-k3",
            api_key="k",
            model_input={},
            payload_overrides={"temperature": 1.0},
        )
        await _post_completion(
            _Client(),
            base_url="http://127.0.0.1:8787/v1",
            model="kimi-k3",
            api_key="k",
            model_input={},
            payload_overrides={"temperature": None},
        )

    asyncio.run(run())
    assert captured[0]["temperature"] == 0.2
    assert captured[1]["temperature"] == 1.0
    assert "temperature" not in captured[2]


def test_report_json_salvages_a_length_truncated_response() -> None:
    # finish_reason == "length": the model hit max_tokens mid-sentence. The lead
    # sections are complete and the tail is cut — keep the head, drop the tail.
    truncated = (
        '{"one_liner":"decode with /predict","vuln_type":"MODEL_INVERSION",'
        '"attack_chain":["GET /model","POST /predict"],'
        '"execution":[{"step":1,"title":"probe","detail":"ran curl"}],'
        '"repro":"```python\\nimport requests\\nr = requests.get(BASE)\\n'
        'd = r.json()\\nprint(d)\\n# then walk the vocab one char at a ti'
    )
    sections = _parse_message(truncated)
    assert sections["one_liner"] == "decode with /predict"
    assert sections["vuln_type"] == "MODEL_INVERSION"
    assert sections["attack_chain"] == ["GET /model", "POST /predict"]
    assert sections["execution"][0]["title"] == "probe"
    # The half-written repro is retained (clipped at the cut point), not fatal.
    assert "requests" in sections["repro"]


def test_report_json_salvage_ignores_braces_inside_strings() -> None:
    # repro bodies are full of braces; nesting must be tracked outside strings
    # or the salvage would close the wrong bracket.
    truncated = '{"one_liner":"x","attack_chain":["a"],"repro":"flag CTF2 inner'
    # Ends mid-string, with the string itself still open.
    sections = _parse_message(truncated)
    assert sections["one_liner"] == "x"
    assert sections["attack_chain"] == ["a"]
    assert sections["repro"] == "flag CTF2 inner"


def test_unparseable_report_json_still_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="LLM_REPORT_JSON_UNPARSEABLE"):
        _parse_message("not json at all, no braces here")


def test_attack_chain_dicts_are_flattened_to_strings() -> None:
    # glm-5.3 returns [{"step": "GET /model"}, ...] where the schema wants
    # ["GET /model", ...]. Rendering str(dict) would print literal Python dicts.
    raw = (
        '{"one_liner":"decode","vuln_type":"MODEL_INVERSION",'
        '"attack_chain":[{"step":"GET /model"},{"step":"POST /predict","extra":"x"}],'
        '"execution":[],"repro":"curl /model","failed_paths":[],"remediation":[]}'
    )
    sections = _parse_message(raw)
    assert sections["attack_chain"] == ["GET /model", "POST /predict"]
    assert all(isinstance(row, str) for row in sections["attack_chain"])


def test_failed_paths_and_remediation_accept_object_items() -> None:
    raw = (
        '{"one_liner":"x","attack_chain":[],"execution":[],"repro":"r",'
        '"failed_paths":[{"description":"top-1 was a canary"}],'
        '"remediation":[{"text":"rate-limit /predict"}]}'
    )
    sections = _parse_message(raw)
    assert sections["failed_paths"] == ["top-1 was a canary"]
    assert sections["remediation"] == ["rate-limit /predict"]
