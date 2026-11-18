"""A-path (chat_completions) Worker: the ``python_run`` action.

``python_run`` exists because one request per tool call cannot express an
oracle that needs hundreds of requests.  These tests pin the three properties
that make it useful: the tool is declared, its output can be submitted as a
flag, and every bound (code size, run budget, wall clock) is enforced.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.solver.muteki.worker import openai_compatible
from muteki.core.cost import CostController
from muteki.core.llm import LLMResponse, ToolCall
from muteki.solver.cli_driver import CliResult


class _Graph:
    def __init__(self) -> None:
        self.facts: list[str] = []

    def snapshot(self):
        return SimpleNamespace(evidence=[])

    def add_evidence(self, *, fact: str, **_kwargs) -> int:
        self.facts.append(fact)
        return len(self.facts)


def _worker(tmp_path, graph=None, *, max_turns: int = 4, timeout_seconds: int = 10):
    return openai_compatible.OpenAICompatibleWorker(
        graph=graph or _Graph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-python",
        intent_id="intent-python",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="deepseek-v4-flash",
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        cost=CostController(),
        run_id="run-python",
    )


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _encode(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _ScriptedClient:
    """Return one scripted response per turn, then stop."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(
            content="done", reasoning="", tool_calls=[], finish_reason="stop", model="m"
        )

    async def aclose(self) -> None:
        return None


def _response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content="",
        reasoning="",
        tool_calls=list(calls),
        finish_reason="tool_calls",
        model="deepseek-v4-flash",
    )


def _ok_payload(stdout: str, *, exit_code: int = 0, timed_out: bool = False) -> str:    return json.dumps(
        {
            "success": exit_code == 0 and not timed_out,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": 0.4,
            "stdout": stdout,
            "stderr": "",
            "error": "",
        }
    )


def test_python_run_is_declared_as_a_worker_tool() -> None:
    names = [tool["function"]["name"] for tool in openai_compatible.WORKER_TOOLS]
    assert "python_run" in names
    schema = next(
        tool for tool in openai_compatible.WORKER_TOOLS
        if tool["function"]["name"] == "python_run"
    )["function"]["parameters"]
    assert schema["required"] == ["code"]
    assert schema["additionalProperties"] is False
    # The old name must keep working: other modules import it.
    assert tuple(openai_compatible.HTTP_TOOLS) == tuple(openai_compatible.WORKER_TOOLS)


def test_python_run_output_can_be_submitted_as_a_flag(tmp_path, monkeypatch) -> None:
    graph = _Graph()
    monkeypatch.setattr(
        openai_compatible,
        "LLMClient",
        lambda **_kwargs: _ScriptedClient(
            [
                _response(_tool_call("python_run", {"code": "print('flag{blind}'"})),
                _response(_tool_call("submit_flag", {"flag": "flag{blind}"}, "call-2")),
            ]
        ),
    )

    def run_command(*_args, **_kwargs):
        return CliResult(text=_ok_payload("extracted: flag{blind}\n"))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)

    worker = _worker(tmp_path, graph)
    result = asyncio.run(worker.run(goal="extract the token"))

    assert result.success is True
    assert worker.verified_flag == "flag{blind}"
    assert result.metadata["python_run_count"] == 1
    # The script outcome has to land in the SharedGraph, otherwise the next
    # Worker re-runs the same script.
    assert any('"tool": "python_run"' in fact for fact in graph.facts)


def test_python_run_rejects_a_flag_it_never_observed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        openai_compatible,
        "LLMClient",
        lambda **_kwargs: _ScriptedClient(
            [_response(_tool_call("submit_flag", {"flag": "flag{invented}"}))]
        ),
    )
    monkeypatch.setattr(
        openai_compatible,
        "run_cli_streaming_container",
        lambda *_args, **_kwargs: CliResult(text=_ok_payload("nothing here")),
    )

    worker = _worker(tmp_path)
    asyncio.run(worker.run(goal="submit"))
    assert worker.verified_flag is None


def test_python_run_enforces_the_per_worker_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        openai_compatible,
        "run_cli_streaming_container",
        lambda *_args, **_kwargs: CliResult(text=_ok_payload("ok")),
    )
    worker = _worker(tmp_path)
    budget = openai_compatible._PYTHON_RUN_BUDGET

    results = [
        asyncio.run(worker._python_run({"code": "print(1)"})) for _ in range(budget + 1)
    ]

    assert all(item["success"] for item in results[:budget])
    assert results[-1]["error"] == "PYTHON_RUN_BUDGET_EXHAUSTED"
    assert worker._result(True, "COMPLETED").metadata["python_run_count"] == budget + 1


def test_python_run_rejects_empty_and_oversized_code(tmp_path, monkeypatch) -> None:
    calls: list[list] = []

    def run_command(*args, **kwargs):
        calls.append(list(args))
        return CliResult(text=_ok_payload(""))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    worker = _worker(tmp_path)

    assert asyncio.run(worker._python_run({"code": "   "}))["error"] == "PYTHON_CODE_REQUIRED"
    oversized = "x" * (openai_compatible._PYTHON_RUN_MAX_CODE + 1)
    assert asyncio.run(worker._python_run({"code": oversized}))["error"] == (
        "PYTHON_CODE_TOO_LARGE"
    )
    assert calls == []


def test_python_run_clamps_the_wall_clock(tmp_path, monkeypatch) -> None:
    seen: dict[str, int] = {}

    def run_command(*_args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return CliResult(text=_ok_payload(""))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    worker = _worker(tmp_path)

    asyncio.run(worker._python_run({"code": "print(1)", "timeout_seconds": 99_999}))
    assert seen["timeout"] == openai_compatible._PYTHON_RUN_MAX_SECONDS + 15

    asyncio.run(worker._python_run({"code": "print(1)", "timeout_seconds": 1}))
    assert seen["timeout"] == 5 + 15


def test_python_run_survives_garbage_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        openai_compatible,
        "run_cli_streaming_container",
        lambda *_args, **_kwargs: CliResult(text="<html>502 bad gateway</html>"),
    )
    worker = _worker(tmp_path)

    observed = asyncio.run(worker._python_run({"code": "print(1)"}))
    assert observed["success"] is False
    assert "502" in observed["stdout"]


def test_python_run_never_sends_a_host_path_into_the_container(tmp_path, monkeypatch) -> None:
    """The executor maps the cwd; a host path in the payload cannot exist there."""

    captured: dict = {}

    def run_command(_driver, argv, **_kwargs):
        captured["config"] = json.loads(base64.b64decode(argv[3]).decode())
        return CliResult(text=_ok_payload("ok"))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    worker = _worker(tmp_path)
    asyncio.run(worker._python_run({"code": "print(1)"}))

    assert "cwd" not in captured["config"]
    assert str(tmp_path) not in json.dumps(captured["config"])


def test_python_run_wrapper_inherits_the_working_directory(tmp_path) -> None:
    payload = _encode({"code": "import os; print(os.getcwd())", "target": "", "timeout": 20})
    completed = subprocess.run(
        [sys.executable, "-c", openai_compatible._PYTHON_RUN_SCRIPT, payload],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed["success"] is True
    assert Path(observed["stdout"].strip()).resolve() == tmp_path.resolve()


class _LockedGraph(_Graph):
    """Sibling Workers already executed every endpoint."""

    def try_claim_activity(self, **_kwargs) -> bool:
        return False


def test_peer_executed_endpoint_does_not_end_the_round_on_the_first_call(
    tmp_path, monkeypatch
) -> None:
    """A Worker assigned a route a sibling touched must still get to work.

    Payload variants share an endpoint key, so the dedupe used to finish the
    round on call one -- the Worker never reached its (correct) intent.
    """

    def explode(*_args, **_kwargs):
        raise AssertionError("a rejected endpoint must not reach the sandbox")

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", explode)
    worker = _worker(tmp_path, _LockedGraph())
    call = {"method": "POST", "url": "/api/warranty/check", "body": "department=OPS"}

    first = asyncio.run(worker._http("http_request", call))
    assert first["error"] == "ENDPOINT_ALREADY_EXECUTED"
    assert first["route_exhausted"] is False
    assert "python_run" in first["hint"]

    second = asyncio.run(worker._http("http_request", call))
    assert second["route_exhausted"] is True
    assert worker.route_exhausted is True


def test_tool_arguments_with_raw_newlines_are_recovered() -> None:
    """A model that pastes multi-line code produces raw newlines in the JSON."""

    parsed, error = openai_compatible._parse_tool_arguments('{"code": "print(1)\nprint(2)"}')
    assert error == ""
    assert parsed["code"] == "print(1)\nprint(2)"


@pytest.mark.parametrize("raw", ['{"code": "print(1)', "", "not json"])
def test_tool_arguments_failures_are_named(raw) -> None:
    parsed, error = openai_compatible._parse_tool_arguments(raw)
    assert parsed == {}
    assert error in {"", "TOOL_ARGUMENTS_NOT_JSON"}


def test_broken_arguments_reach_the_model_as_an_error(tmp_path, monkeypatch) -> None:
    """An unparseable call must not look like a call with no arguments."""

    seen: list[dict] = []

    class _RecordingClient:
        def __init__(self, **_kwargs) -> None:
            self.calls = 0

        async def chat(self, **kwargs):
            seen.append({"messages": list(kwargs["messages"])})
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    reasoning="",
                    tool_calls=[
                        ToolCall(id="call-x", name="python_run", arguments='{"code": "print(1)')
                    ],
                    finish_reason="tool_calls",
                    model="m",
                )
            return LLMResponse(
                content="stopping", reasoning="", tool_calls=[], finish_reason="stop", model="m"
            )

        async def aclose(self) -> None:
            return None

    def explode(*_args, **_kwargs):
        raise AssertionError("a broken call must not reach the sandbox")

    monkeypatch.setattr(openai_compatible, "LLMClient", lambda **_kw: _RecordingClient())
    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", explode)

    worker = _worker(tmp_path)
    asyncio.run(worker.run(goal="script"))

    tool_message = next(
        message for message in seen[1]["messages"] if message.get("role") == "tool"
    )
    assert "TOOL_ARGUMENTS_NOT_JSON" in tool_message["content"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print('hello')", "hello"),
        ("import sys; sys.exit(3)", ""),
    ],
)
def test_python_run_wrapper_executes_real_code(tmp_path, code, expected) -> None:
    """Run the wrapper exactly as the container would (same argv shape)."""

    payload = _encode(
        {"code": code, "cwd": str(tmp_path), "target": "http://t/", "timeout": 20}
    )
    completed = subprocess.run(
        [sys.executable, "-c", openai_compatible._PYTHON_RUN_SCRIPT, payload],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert expected in observed["stdout"]
    if "sys.exit(3)" in code:
        assert observed["exit_code"] == 3
        assert observed["success"] is False


def test_python_run_wrapper_writes_into_the_workspace(tmp_path) -> None:
    code = "open('notes.txt','w').write('kept')"
    payload = _encode(
        {"code": code, "cwd": str(tmp_path), "target": "", "timeout": 20}
    )
    subprocess.run(
        [sys.executable, "-c", openai_compatible._PYTHON_RUN_SCRIPT, payload],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "kept"


def test_python_run_wrapper_reports_timeout(tmp_path) -> None:
    payload = _encode(
        {
            "code": "import time\ntime.sleep(30)",
            "cwd": str(tmp_path),
            "target": "",
            "timeout": 5,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", openai_compatible._PYTHON_RUN_SCRIPT, payload],
        capture_output=True,
        text=True,
        timeout=90,
    )
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed["timed_out"] is True
    assert observed["success"] is False
