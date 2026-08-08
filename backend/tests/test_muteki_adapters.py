from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.schemas.run import RunCreate
from app.solver.muteki.adapter.event_bridge import EventBridge
from app.solver.muteki.adapter.evidence_adapter import EvidenceAdapter
from app.solver.muteki.adapter.runner_adapter import RunnerAdapter
from app.solver.muteki.adapter.tool_adapter import ToolAdapter
from app.solver.muteki.core.orchestrator import MutekiOrchestrator
from app.solver.muteki.events import EventEnvelope
from app.solver.muteki.graph import Fact, MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.workers import WorkerOutcome


@dataclass
class FakeWorkerResult:
    success: bool = True
    output: dict = None
    metadata: dict = None
    evidence_refs: list[str] = None


class FakeGatewayWorker:
    async def execute(self, action):
        return FakeWorkerResult(True, {"status": "COMPLETED", "summary": "baseline"}, {"tool_call_id": "call-1"}, ["ev-1"])


class FakeRunner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "COMPLETED", "stdout": "ok"}

    async def create_job(self, run_id, allowed_hosts, tool, arguments):
        self.calls.append((run_id, allowed_hosts, tool, arguments))
        return "job-1"

    async def wait_job(self, job_id, **kwargs):
        return self.result


class FakeEventService:
    def __init__(self):
        self.events = []

    async def append(self, session, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload))
        return payload


def test_tool_adapter_normalizes_gateway_result_without_raw_fact():
    adapter = object.__new__(ToolAdapter)
    adapter._worker = FakeGatewayWorker()
    result = asyncio.run(adapter.execute_tool("http_request", {"url": "http://target"}, "ws", "run"))
    assert result.success is True
    fact = adapter.to_fact(result)
    assert fact.verified is True
    assert "response" not in fact.content
    assert fact.evidence_refs == ("ev-1",)


def test_runner_adapter_normalizes_python_job():
    runner = FakeRunner()
    adapter = RunnerAdapter(runner)
    result = asyncio.run(adapter.run_python("print(1)", "ws", "run"))
    assert result.success is True
    assert runner.calls[0][2] == "python_run"
    assert runner.calls[0][3]["network_mode"] == "none"


def test_runner_adapter_returns_data_on_runner_failure():
    runner = FakeRunner({"status": "FAILED", "error_code": "RUNNER_TIMEOUT"})
    result = asyncio.run(RunnerAdapter(runner).run_script("x.py", [], "ws", "run"))
    assert result.success is False
    assert result.error_code == "RUNNER_TIMEOUT"


def test_evidence_adapter_preserves_authoritative_reference():
    class Authority:
        async def verify_refs(self, refs, *, run_id):
            return refs == ["ev-1"] and run_id == "run"

    fact = Fact(0, "safe summary", "worker", True, "", ("ev-1",))
    assert asyncio.run(EvidenceAdapter(Authority()).write_fact(fact, "run", "worker")) == "ev-1"


def test_event_bridge_deduplicates_and_sanitizes_sensitive_payload():
    service = FakeEventService()
    bridge = EventBridge(object(), service=service, run_id="run")
    event = EventEnvelope(3, "now", "challenge", "worker", "fact_added", {"body": "secret", "evidence_refs": ["ev-1"]})
    assert asyncio.run(bridge.bridge(event)) is not None
    assert asyncio.run(bridge.bridge(event)) is None
    assert len(service.events) == 1
    assert "body" not in service.events[0][2]["payload"]
    assert service.events[0][2]["payload"]["evidence_refs"] == ["ev-1"]


def test_muteki_graph_intent_payload_survives_projection_rebuild(tmp_path):
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    intent_id = graph.propose_intent(actor="reason", description="baseline", payload={"tool_name": "http_request"})
    graph.rebuild_projections()
    assert graph.intents()[0].intent_id == intent_id
    assert graph.intents()[0].payload == {"tool_name": "http_request"}


def test_muteki_orchestrator_reaches_verified_flag(tmp_path):
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")

    async def worker(job):
        graph.add_fact(actor=job.worker_id, content="verified evidence", verified=True, evidence_refs=["ev-1"])
        graph.write_flag(actor=job.worker_id, flag="flag{real}", real_output="flag{real}")
        return WorkerOutcome(job.worker_id, "COMPLETED", flag_found=True, result="ok")

    result = asyncio.run(MutekiOrchestrator(graph, MutekiReason(), worker_runner=worker).run(max_rounds=2))
    assert result.status == "COMPLETED_SOLVED"
    assert graph.flags(verified_only=True)[0].flag_value == "flag{real}"


def test_run_create_accepts_muteki_without_changing_default():
    assert RunCreate().solver_mode == "multi_agent_v1"
    assert RunCreate(solver_mode="muteki").solver_mode == "muteki"


def test_event_bridge_callback_flushes_in_canonical_order():
    service = FakeEventService()
    bridge = EventBridge(object(), service=service, run_id="run")
    callback = bridge.callback()
    callback(EventEnvelope(1, "now", "run", "a", "one", {}))
    callback(EventEnvelope(2, "now", "run", "a", "two", {}))
    asyncio.run(bridge.flush())
    assert [item[2]["muteki_sequence"] for item in service.events] == [1, 2]


def test_runner_adapter_truncates_oversized_output():
    runner = FakeRunner({"status": "COMPLETED", "stdout": "x" * 2000})
    result = asyncio.run(RunnerAdapter(runner, max_output_chars=1000).run_python("print(1)", "ws", "run"))
    assert result.output["truncated"] is True
