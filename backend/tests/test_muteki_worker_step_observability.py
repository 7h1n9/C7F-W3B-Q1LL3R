"""A Worker round must not be a black box.

``EventType.WORKER_STEP`` was defined, the Coordinator callback
(``_record_worker_step``) was wired into ``OfficialWorkerAdapter``, and yet
``run_events`` held **zero** ``muteki.worker_step`` rows across every Run.

The reason was a dispatch fork: the adapter only attached its step callback on
the legacy one-shot path.  Production runs ``protocol="cli_solver"``, which goes
through ``_execute_cli_solver_sync`` -> vendored ``CliSolver``; that solver
defines its own in-method ``on_step`` and exposes no injection point, so nothing
ever reached the Coordinator.  A 12-minute Worker round therefore produced no
intermediate record at all, and "why did it fail?" was unanswerable.

These tests pin the two halves of the fix: the driver-level observer that makes
the vendored path observable, and the per-Worker coalescing that keeps
observability from becoming the dominant write load.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.solver.muteki.coordinator import (
    WORKER_STEP_MAX_TOOLS,
    WORKER_STEP_MIN_INTERVAL_SECONDS,
    MutekiCoordinator,
)
from app.solver.muteki.events import EventType
from app.solver.muteki.worker.official_worker import (
    OfficialWorkerAdapter,
    _attach_step_observer,
)


class _Step:
    def __init__(self, kind: str, *, tool: str = "", session: str = "", text: str = "") -> None:
        self.kind = kind
        self.tool = tool
        self.session = session
        self.text = text


class _Driver:
    """Minimal stand-in for a vendored CLI driver."""

    def __init__(self, steps_by_line: dict[str, list[_Step]]) -> None:
        self._steps_by_line = steps_by_line
        self.calls: list[str] = []

    def parse_stream_steps(self, line: str) -> list[_Step]:
        self.calls.append(line)
        return list(self._steps_by_line.get(line, []))


# ── the driver-level observer ────────────────────────────────────────────────


def test_observer_forwards_every_parsed_step() -> None:
    driver = _Driver({"a": [_Step("reasoning"), _Step("tool", tool="curl")]})
    seen: list[_Step] = []

    _attach_step_observer(driver, seen.append)

    returned = driver.parse_stream_steps("a")

    assert [step.kind for step in seen] == ["reasoning", "tool"]
    assert seen[1].tool == "curl"
    # The solver must still receive the parsed steps: observation is additive.
    assert [step.kind for step in returned] == ["reasoning", "tool"]
    assert driver.calls == ["a"]


def test_observer_receives_nothing_for_a_line_without_steps() -> None:
    driver = _Driver({})
    seen: list[_Step] = []

    _attach_step_observer(driver, seen.append)

    assert driver.parse_stream_steps("noise") == []
    assert seen == []


def test_observer_failure_never_breaks_the_worker() -> None:
    """A broken observability path must degrade to silence, not kill the Worker."""

    driver = _Driver({"a": [_Step("tool", tool="curl")]})

    def _explode(_step: object) -> None:
        raise RuntimeError("observability is down")

    _attach_step_observer(driver, _explode)

    assert [step.kind for step in driver.parse_stream_steps("a")] == ["tool"]


def test_observer_does_not_mutate_the_shared_driver_class() -> None:
    """``driver_for`` returns a process-wide singleton; only the copy is patched."""

    original = _Driver.parse_stream_steps
    driver = _Driver({"a": [_Step("tool", tool="curl")]})
    seen: list[_Step] = []

    _attach_step_observer(driver, seen.append)
    driver.parse_stream_steps("a")

    assert _Driver.parse_stream_steps is original
    assert seen, "the patched instance should still be observed"


def test_observer_is_skipped_when_absent_or_driver_is_unsuitable() -> None:
    driver = _Driver({"a": [_Step("tool")]})

    assert _attach_step_observer(driver, None) is driver
    assert driver.parse_stream_steps("a")[0].kind == "tool"

    class _NoParser:
        pass

    stub = _NoParser()
    assert _attach_step_observer(stub, lambda _s: None) is stub


# ── the adapter payload stays identity-only ──────────────────────────────────


def test_on_step_records_output_without_leaking_credentials() -> None:
    """Tool output is now recorded -- but credentials inside it are masked.

    The old contract republished nothing, which made every "why did this fail?"
    question unanswerable.  The trade-off is redaction at the boundary instead
    of omission: the trace shows what ran, never the secret it carried.
    """

    from app.solver.muteki.worker.official_worker import OfficialWorkerAdapter

    captured: list[dict[str, str]] = []
    adapter = object.__new__(OfficialWorkerAdapter)
    adapter.step_callback = captured.append
    job = SimpleNamespace(worker_id="w-1")
    step = _Step(
        "tool_result",
        tool="sqlmap",
        session="s-9",
        text="db is up; password=hunter2; FLAG=CTF2{real_flag}",
    )

    adapter._on_step(job, step)

    assert len(captured) == 1
    payload = captured[0]
    assert payload["worker_id"] == "w-1"
    assert payload["kind"] == "tool_result"
    assert payload["tool"] == "sqlmap"
    assert payload["session"] == "s-9"
    # The useful part of the output survives...
    assert "db is up" in payload["result_excerpt"]
    # ...including the flag itself, which is the whole point of the trace...
    assert "CTF2{real_flag}" in payload["result_excerpt"]
    # ...but the credential does not.
    assert "hunter2" not in str(captured)
    assert "[redacted]" in payload["result_excerpt"]


# ── the per-Worker coalescing ────────────────────────────────────────────────


class _Graph:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def emit_event(self, *, actor: str, event_type: str, payload: dict) -> int:
        self.events.append((actor, str(event_type), dict(payload)))
        return len(self.events)

    def worker_step_events(self) -> list[dict]:
        return [
            payload
            for _actor, event_type, payload in self.events
            if event_type == str(EventType.WORKER_STEP)
        ]


def _coordinator() -> MutekiCoordinator:
    """A Coordinator holding only the fields the step recorder touches."""

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = _Graph()
    coordinator._worker_step_state = {}
    import threading

    coordinator._worker_step_lock = threading.Lock()
    return coordinator


def test_first_step_is_published_immediately() -> None:
    coordinator = _coordinator()

    coordinator._record_worker_step(
        {"worker_id": "w-1", "kind": "reasoning", "tool": "", "session": ""}
    )

    events = coordinator.graph.worker_step_events()
    assert len(events) == 1
    assert events[0]["kind"] == "reasoning"
    assert events[0]["steps_total"] == 1
    assert events[0]["steps_in_window"] == 1


def test_repeated_steps_in_the_window_are_coalesced() -> None:
    coordinator = _coordinator()

    for _ in range(25):
        coordinator._record_worker_step(
            {"worker_id": "w-1", "kind": "reasoning", "tool": "", "session": ""}
        )

    # 25 identical steps inside one cadence window must not become 25 rows.
    assert len(coordinator.graph.worker_step_events()) == 1


def test_kind_change_publishes_even_inside_the_window() -> None:
    coordinator = _coordinator()

    coordinator._record_worker_step({"worker_id": "w-1", "kind": "reasoning"})
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "curl"})
    coordinator._record_worker_step(
        {"worker_id": "w-1", "kind": "tool_result", "tool": "curl"}
    )

    kinds = [payload["kind"] for payload in coordinator.graph.worker_step_events()]
    assert kinds == ["reasoning", "tool", "tool_result"]


def test_aggregates_accumulate_across_suppressed_steps(monkeypatch) -> None:
    coordinator = _coordinator()
    now = {"t": 1000.0}
    monkeypatch.setattr(
        "app.solver.muteki.coordinator.time.monotonic", lambda: now["t"]
    )

    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "curl"})
    # Same kind, same window: folded into the aggregate.
    now["t"] += 1.0
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "nmap"})
    now["t"] += 1.0
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "curl"})
    # Window elapsed: the aggregate is published.
    now["t"] += WORKER_STEP_MIN_INTERVAL_SECONDS
    coordinator._record_worker_step(
        {"worker_id": "w-1", "kind": "tool", "tool": "sqlmap", "session": "s-1"}
    )

    events = coordinator.graph.worker_step_events()
    assert len(events) == 2
    latest = events[-1]
    assert latest["steps_total"] == 4
    assert latest["steps_in_window"] == 3
    assert latest["tool_steps"] == 4
    assert latest["distinct_tools"] == ["curl", "nmap", "sqlmap"]
    assert latest["session"] == "s-1"


def test_distinct_tools_is_bounded(monkeypatch) -> None:
    coordinator = _coordinator()
    now = {"t": 1000.0}
    monkeypatch.setattr(
        "app.solver.muteki.coordinator.time.monotonic", lambda: now["t"]
    )

    for index in range(WORKER_STEP_MAX_TOOLS + 8):
        # Each tool lands in its own cadence window so it is actually published.
        now["t"] += WORKER_STEP_MIN_INTERVAL_SECONDS
        coordinator._record_worker_step(
            {"worker_id": "w-1", "kind": "tool", "tool": f"tool-{index}"}
        )

    latest = coordinator.graph.worker_step_events()[-1]
    assert latest["tool_steps"] == WORKER_STEP_MAX_TOOLS + 8
    assert len(latest["distinct_tools"]) == WORKER_STEP_MAX_TOOLS


def test_workers_are_coalesced_independently() -> None:
    coordinator = _coordinator()

    coordinator._record_worker_step({"worker_id": "w-1", "kind": "reasoning"})
    coordinator._record_worker_step({"worker_id": "w-2", "kind": "reasoning"})

    actors = [actor for actor, _event, _payload in coordinator.graph.events]
    assert actors == ["w-1", "w-2"]


# ── the trailing flush ───────────────────────────────────────────────────────


def test_flush_publishes_the_trailing_aggregate_then_stops() -> None:
    coordinator = _coordinator()

    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "curl"})
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool", "tool": "nmap"})
    coordinator._flush_worker_steps("w-1", result="FAILED")

    events = coordinator.graph.worker_step_events()
    assert [payload["kind"] for payload in events] == ["tool", "worker_finished"]
    assert events[-1]["worker_result"] == "FAILED"
    assert events[-1]["steps_total"] == 2
    # The tail that was still coalesced is reported, not lost.
    assert events[-1]["steps_in_window"] == 1

    # Idempotent: state was dropped, so a second flush is a no-op.
    coordinator._flush_worker_steps("w-1", result="FAILED")
    assert len(coordinator.graph.worker_step_events()) == 2


def test_flush_of_an_unknown_worker_is_a_noop() -> None:
    coordinator = _coordinator()

    coordinator._flush_worker_steps("never-seen", result="COMPLETED")

    assert coordinator.graph.worker_step_events() == []


@pytest.mark.parametrize("result", ["COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"])
def test_flush_records_the_worker_result(result: str) -> None:
    coordinator = _coordinator()
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "reasoning"})

    coordinator._flush_worker_steps("w-1", result=result)

    assert coordinator.graph.worker_step_events()[-1]["worker_result"] == result


# ── the second dispatch fork: the Chat Completions Worker ────────────────────
#
# ``protocol="cli_solver"`` was not the only black box.  ``_execute_sync`` forks
# to ``_execute_openai_compatible_sync`` whenever the driver profile declares
# ``protocol="chat_completions"`` — and that Worker runs the official Muteki
# ``LLMClient`` loop, never touching ``CliSolver`` or
# ``driver.parse_stream_steps``.  So a Run whose Worker engine is
# ``openai_compatible`` (the shipped default) still wrote zero WORKER_STEP rows
# even after the driver-level observer landed.  These tests pin the reporting
# point and its wiring.


def _chat_worker(**overrides: Any):
    from muteki.core.cost import CostController

    from app.solver.muteki.worker.openai_compatible import OpenAICompatibleWorker

    kwargs: dict[str, Any] = {
        "graph": None,
        "container": object(),
        "workspace": ".",
        "worker_id": "w-chat-1",
        "intent_id": None,
        "target_url": "http://target.example",
        "base_url": "http://relay.example/v1",
        "api_key": "sk-test",
        "model": "deepseek-flash",
        "max_turns": 1,
        "timeout_seconds": 10,
        "cost": CostController(),
        "run_id": "run-1",
    }
    kwargs.update(overrides)
    return OpenAICompatibleWorker(**kwargs)


@pytest.mark.asyncio
async def test_chat_worker_reports_every_tool_dispatch(monkeypatch) -> None:
    """Every tool call publishes exactly one record: command + result together."""

    reported: list[dict[str, str]] = []
    worker = _chat_worker(step_callback=reported.append)

    async def _fake_http(_tool: str, _arguments: dict) -> dict:
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(worker, "_http", _fake_http)

    await worker._dispatch_tool_observed(
        "http_request", {"method": "GET", "url": "/a?x=1"}, call_id="c1"
    )
    await worker._dispatch_tool_observed("submit_flag", {"flag": "CTF2{x}"}, call_id="c2")
    await worker._dispatch_tool_observed("no_such_tool", {}, call_id="c3")

    assert [payload["tool"] for payload in reported] == [
        "http_request",
        "submit_flag",
        "no_such_tool",
    ]
    assert {payload["kind"] for payload in reported} == {"tool"}
    assert {payload["worker_id"] for payload in reported} == {"w-chat-1"}
    # The command is recorded, not just the tool name.
    assert reported[0]["command"] == "http_request: GET /a?x=1"
    # ...and so is the outcome, in the same record.
    assert "status_code" in reported[0]["result_excerpt"]
    assert [payload.get("call_id") for payload in reported] == ["c1", "c2", "c3"]


def test_chat_worker_step_report_carries_no_target_data() -> None:
    reported: list[dict[str, str]] = []
    worker = _chat_worker(step_callback=reported.append)

    # The tool name is the whole payload: no arguments, no response body.
    worker._report_step("tool", "http_request")

    assert reported == [
        {
            "worker_id": "w-chat-1",
            "kind": "tool",
            "tool": "http_request",
            "session": "",
        }
    ]


def test_chat_worker_step_report_is_failure_isolated() -> None:
    def _boom(_payload: dict[str, str]) -> None:
        raise RuntimeError("sink down")

    # Observability must never abort a Worker round.
    _chat_worker(step_callback=_boom)._report_step("tool", "http_request")


def test_chat_worker_step_report_without_a_sink_is_a_noop() -> None:
    _chat_worker(step_callback=None)._report_step("turn")


class _RecordingAdapter:
    """The two adapter methods the wiring test needs, over a real callback."""

    def __init__(self, callback) -> None:
        self.step_callback = callback

    _emit_step_payload = OfficialWorkerAdapter._emit_step_payload


def test_emit_step_payload_forwards_to_the_step_callback() -> None:
    seen: list[dict] = []
    adapter = OfficialWorkerAdapter(step_callback=seen.append)

    adapter._emit_step_payload(
        {"worker_id": "w-1", "kind": "tool", "tool": "http_request"}
    )

    assert seen == [{"worker_id": "w-1", "kind": "tool", "tool": "http_request"}]


def test_emit_step_payload_isolates_a_failing_sink() -> None:
    def _boom(_payload: dict) -> None:
        raise RuntimeError("sink down")

    # No sink may be able to turn a Worker round into a failure.
    OfficialWorkerAdapter(step_callback=_boom)._emit_step_payload({"kind": "tool"})


def test_emit_step_payload_without_a_sink_is_a_noop() -> None:
    OfficialWorkerAdapter(step_callback=None)._emit_step_payload({"kind": "tool"})


def test_chat_completions_dispatch_wires_the_step_sink(monkeypatch) -> None:
    """The regression this section exists for.

    ``_execute_openai_compatible_sync`` used to construct its Worker with no step
    sink at all, so the ``chat_completions`` fork could never emit WORKER_STEP.
    """

    seen: list[dict] = []
    adapter = OfficialWorkerAdapter(step_callback=seen.append)
    captured: dict[str, Any] = {}

    class _FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.step_callback = kwargs.get("step_callback")

        async def run(self, *, goal: str, payload: dict):
            from app.solver.muteki.worker.official_worker import (
                OfficialWorkerResult,
            )

            assert self.step_callback is not None, "no step sink was wired in"
            # What the real Worker does on every bounded action.
            self.step_callback(
                {
                    "worker_id": "w-chat-1",
                    "kind": "tool",
                    "tool": "http_request",
                    "session": "",
                }
            )
            return OfficialWorkerResult(True, "COMPLETED", "deepseek-flash")

    import app.solver.muteki.worker.openai_compatible as module

    monkeypatch.setattr(module, "OpenAICompatibleWorker", _FakeWorker)

    job = SimpleNamespace(
        worker_id="w-chat-1",
        intent_id="intent-1",
        challenge_id="challenge-1",
        engine_id="openai-compatible:cfg-1",
        goal="probe the target",
        payload={},
        driver_profile={
            "protocol": "chat_completions",
            "model": "deepseek-flash",
            "base_url": "http://relay.example/v1",
        },
        environment={
            "MUTEKI_TARGET_URL": "http://target.example",
            "MUTEKI_WORKSPACE": "/tmp/workspace",
            "OPENAI_API_KEY": "sk-test",
        },
    )

    result = adapter._execute_openai_compatible_sync(job, dict(job.driver_profile))

    assert result.success is True
    assert captured["step_callback"] is not None
    assert seen and seen[0]["tool"] == "http_request"


# ── publishing from the Worker's thread (the second, deeper root cause) ───────
#
# ``emit_event`` fans out through ``EventBridge.callback().submit()``, which
# schedules the durable write as a task on the loop running in the *calling*
# thread.  A Worker reports steps while its own short-lived ``asyncio.run`` loop
# is active, so the write was scheduled on a loop that gets torn down the moment
# the Worker returns -- and cancelled, silently.  ``phase_changed`` (emitted by
# the Coordinator, already on the runtime loop) persisted; ``worker_step`` did
# not.  That asymmetry is the evidence these tests encode.


@pytest.mark.asyncio
async def test_a_step_from_a_foreign_loop_is_marshalled_onto_the_runtime_loop() -> None:
    coordinator = _coordinator()
    coordinator._bind_runtime_loop()
    runtime_thread = threading.current_thread().name
    emitted_from: list[str] = []

    class _RecordingGraph(_Graph):
        def emit_event(self, *, actor: str, event_type: str, payload: dict) -> int:
            emitted_from.append(threading.current_thread().name)
            return super().emit_event(
                actor=actor, event_type=event_type, payload=payload
            )

    coordinator.graph = _RecordingGraph()

    def _worker_thread() -> None:
        # What a Worker thread looks like: its own loop, gone as soon as it
        # returns.  A task scheduled here would be cancelled on the way out.
        asyncio.run(_emit_from_a_short_lived_loop(coordinator, emitted_from))

    await asyncio.to_thread(_worker_thread)

    # Emitted, but never on the Worker's thread -- the write was handed to the
    # loop that outlives the Worker.
    assert emitted_from == [runtime_thread]
    assert [payload["kind"] for payload in coordinator.graph.worker_step_events()] == [
        "tool"
    ]


async def _emit_from_a_short_lived_loop(coordinator, emitted_from: list[str]) -> None:
    inner = asyncio.get_running_loop()
    coordinator._record_worker_step({"worker_id": "w-foreign", "kind": "tool", "tool": "x"})
    # The inner loop is NOT the runtime loop, so nothing can have been written
    # here yet; whatever lands, lands over on the runtime loop.
    assert inner is not coordinator._runtime_loop_ref
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_step_from_a_thread_with_no_loop_is_marshalled_onto_the_runtime_loop() -> None:
    """The CLI/container Worker's real shape: no loop at all in the caller.

    ``CliSolver._run_streaming`` hands the subprocess to ``asyncio.to_thread``,
    so ``on_step`` fires on a plain thread that never runs a loop.  The previous
    fallback treated that as "not ours to schedule on" and emitted locally --
    and ``EventBridge.submit`` buffers when it cannot see a running loop, so
    every step of the round sat in the buffer until ``flush()`` at Run teardown.
    Symptom in production: a Worker that looked frozen for 13 minutes and then
    wrote 63 steps into one second.
    """

    coordinator = _coordinator()
    coordinator._bind_runtime_loop()
    runtime_thread = threading.current_thread().name
    emitted_from: list[str] = []

    class _RecordingGraph(_Graph):
        def emit_event(self, *, actor: str, event_type: str, payload: dict) -> int:
            emitted_from.append(threading.current_thread().name)
            return super().emit_event(
                actor=actor, event_type=event_type, payload=payload
            )

    coordinator.graph = _RecordingGraph()

    def _no_loop_thread() -> None:
        assert threading.current_thread().name != runtime_thread
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        coordinator._record_worker_step(
            {"worker_id": "w-thread", "kind": "tool", "tool": "curl"}
        )

    await asyncio.to_thread(_no_loop_thread)
    await asyncio.sleep(0.05)

    assert emitted_from == [runtime_thread]
    assert [payload["kind"] for payload in coordinator.graph.worker_step_events()] == [
        "tool"
    ]


@pytest.mark.asyncio
async def test_a_step_from_the_runtime_loop_is_emitted_directly() -> None:
    coordinator = _coordinator()
    coordinator._bind_runtime_loop()

    coordinator._record_worker_step({"worker_id": "w-here", "kind": "tool", "tool": "x"})

    events = coordinator.graph.worker_step_events()
    assert len(events) == 1 and events[0]["kind"] == "tool"


def test_binding_the_runtime_loop_outside_a_loop_is_a_noop() -> None:
    coordinator = _coordinator()

    coordinator._bind_runtime_loop()

    assert getattr(coordinator, "_runtime_loop_ref", None) is None


def test_a_step_without_a_bound_loop_falls_back_to_a_direct_emit() -> None:
    """Never lose the event just because the loop was never bound."""

    coordinator = _coordinator()

    coordinator._record_worker_step({"worker_id": "w-noloop", "kind": "tool", "tool": "x"})

    assert coordinator.graph.worker_step_events()[0]["kind"] == "tool"


def test_a_graph_without_emit_event_is_tolerated() -> None:
    coordinator = _coordinator()
    coordinator.graph = SimpleNamespace()

    coordinator._record_worker_step({"worker_id": "w-1", "kind": "tool"})

    assert not hasattr(coordinator.graph, "emit_event")


# ── per-invocation command records ───────────────────────────────────────────


def _command_events(coordinator: MutekiCoordinator) -> list[dict]:
    return [
        payload
        for _actor, event_type, payload in coordinator.graph.events
        if event_type == str(EventType.WORKER_COMMAND)
    ]


def test_every_tool_command_is_recorded_and_never_coalesced() -> None:
    """Five rapid tool calls must produce five records, not one aggregate.

    Coalescing is right for progress *steps* and wrong for commands: the whole
    value of the record is that it identifies the single call that produced a
    dead end.  These arrive inside the 5s window, which used to collapse them.
    """

    coordinator = _coordinator()

    for index in range(5):
        coordinator._record_worker_step(
            {
                "worker_id": "w-1",
                "kind": "tool",
                "tool": "shell",
                "call_id": f"call-{index}",
                "command": f"curl -s http://t/{index}",
                "result_excerpt": f'{{"status": {200 + index}}}',
            }
        )

    commands = _command_events(coordinator)
    assert len(commands) == 5
    assert [item["call_id"] for item in commands] == [f"call-{i}" for i in range(5)]
    assert [item["command"] for item in commands] == [
        f"curl -s http://t/{i}" for i in range(5)
    ]
    assert commands[0]["result_excerpt"].startswith('{"status"')
    # The aggregate stream still exists alongside them (progress + detail).
    assert coordinator.graph.worker_step_events()


def test_command_records_are_bounded_per_worker() -> None:
    """A pathological loop must not turn the durable stream into a log sink."""

    from app.solver.muteki.coordinator import WORKER_COMMAND_MAX_PER_WORKER

    coordinator = _coordinator()
    for index in range(WORKER_COMMAND_MAX_PER_WORKER + 25):
        coordinator._record_worker_step(
            {
                "worker_id": "w-1",
                "kind": "tool",
                "tool": "python_run",
                "command": f"print({index})",
            }
        )

    assert len(_command_events(coordinator)) == WORKER_COMMAND_MAX_PER_WORKER


def test_steps_without_command_stay_aggregate_only() -> None:
    coordinator = _coordinator()
    coordinator._record_worker_step({"worker_id": "w-1", "kind": "reasoning"})

    assert _command_events(coordinator) == []
    assert coordinator.graph.worker_step_events()
