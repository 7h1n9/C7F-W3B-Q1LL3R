from app.solver.blackboard import Blackboard
from app.solver.coordinator import Coordinator
from app.solver.planner import SolverIntent
from app.solver.state_machine import SolverPhase, TaskStateMachine
from app.solver.worker import WorkerResult


def test_blackboard_initializes_and_versions_updates() -> None:
    board = Blackboard()
    initial = board.initialize("run-1")

    assert initial.phase == SolverPhase.BASELINE
    assert initial.version == 0
    assert board.read("run-1").allowed_actions == []

    updated = board.update("run-1", facts=[{"key": "baseline", "value": True}])

    assert updated.version == 1
    assert updated.facts == [{"key": "baseline", "value": True}]


def test_state_machine_exposes_phase_scoped_actions() -> None:
    board = Blackboard()
    machine = TaskStateMachine()

    for phase, expected in {
        SolverPhase.BASELINE: ["http_request"],
        SolverPhase.VALIDATION: ["sql_boolean_compare", "oracle_probe_matrix"],
        SolverPhase.EXPLOITATION: ["mysql_metadata_discovery", "sql_extract"],
        SolverPhase.IMPACT: ["impact_validation"],
        SolverPhase.REPORTING: ["report"],
    }.items():
        state = board.initialize(f"{phase}", phase=phase)
        assert machine.allowed_actions(state) == expected


class FakePlanner:
    def __init__(self, intent: SolverIntent | None) -> None:
        self.intent = intent

    def choose(self, state, allowed_actions):
        return self.intent


class FakeWorker:
    async def execute(self, intent):
        return WorkerResult(
            status="COMPLETED",
            facts=[{"key": "baseline", "value": True}],
            evidence_refs=["evidence-1"],
        )


async def test_coordinator_runs_one_read_plan_act_observe_write_tick() -> None:
    board = Blackboard()
    board.initialize("run-1")
    coordinator = Coordinator(
        board,
        planner=FakePlanner(SolverIntent("http_request", {"url": "http://target/"})),
        worker=FakeWorker(),
    )

    step = await coordinator.step("run-1")

    assert step.status == "CONTINUE"
    assert step.intent is not None
    assert step.intent.action == "http_request"
    assert step.result is not None
    assert step.state.facts == [{"key": "baseline", "value": True}]
    assert step.state.evidence_refs == ["evidence-1"]
    assert [event["type"] for event in step.state.history] == ["worker.observed"]


async def test_coordinator_rejects_action_outside_phase_without_worker_call() -> None:
    board = Blackboard()
    board.initialize("run-1")
    coordinator = Coordinator(
        board,
        planner=FakePlanner(SolverIntent("sql_extract")),
        worker=FakeWorker(),
    )

    step = await coordinator.step("run-1")

    assert step.status == "REJECTED"
    assert step.result is None
    assert step.state.history[-1]["type"] == "intent.rejected"
