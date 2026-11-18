"""Backend-owned continuous driver for multi_agent_v1 Runs."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, EvidenceLedger, PlannerProposal, VerifiedFact
from app.models.run import (
    FlagCandidate,
    RunAttempt,
    RunContinuation,
    RunEvent,
    RunExecutionLease,
    RunUserInput,
    SolveRun,
    ToolCall,
)
from app.models.solver_state import SolverState
from app.orchestration.state_machine import TERMINAL, RunStatus, parse_run_status, transition
from app.services.continuations import continuation_service
from app.services.events import event_service
from app.services.execution_recovery import execution_recovery_guard
from app.services.flags import flag_service
from app.services.reports import report_service
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer
from app.services.stage_decider import stage_decider
from app.services.supervisor_progress import supervisor_progress_evaluator
from app.services.user_input_consumer import consume_user_inputs
from app.services.writeup_builder import writeup_builder
from app.solver.muteki.adapter.cost_bridge import load_muteki_usage

USER_VISIBLE_TERMINAL = {"WAITING_USER", "COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}
MUTEKI_ATTEMPT_HEARTBEAT_SECONDS = 15.0
MUTEKI_CANCEL_GRACE_SECONDS = 30.0
# ``MutekiCoordinator.finalize`` is the only authority that closes the Solver
# loop (RUN_FINISHED, Worker cancellation and Sandbox teardown).  Cancelling
# the outer supervisor task is not the same as completing that handoff, so the
# outer layer keeps a second, Coordinator-owned window before it lets any
# caller turn the Run terminal.  Only a Run that exceeds both windows is
# force-closed, and that closure always leaves durable evidence.
# This ceiling must not be shorter than the Runtime's own
# ``FINALIZE_GRACE_SECONDS`` (muteki_runtime.py), because the Runtime absorbs an
# outer cancellation and keeps awaiting the same Finalize for that long.  A
# shorter window here would report a forced closure while the Coordinator was
# still finishing, mislabelling a completed handoff as GRACE_EXCEEDED.
MUTEKI_FINALIZE_GRACE_SECONDS = 180.0
# After the durable RUN_FINISHED handoff lands, the cancelled task still has to
# unwind its own ``finally`` blocks (event flush, model/graph close, attempt
# accounting) before the Run is closed cleanly.
MUTEKI_FINALIZE_SETTLE_SECONDS = 5.0
MUTEKI_NATIVE_TERMINAL_EVENT = "muteki.run_finished"
MUTEKI_TERMINAL_RECONCILE_EVENT = "run.muteki_terminal_reconciled"
MUTEKI_FINALIZE_GRACE_EXCEEDED_EVENT = "run.muteki_finalize_grace_exceeded"
logger = logging.getLogger(__name__)


async def _reconcile_muteki_run_counters(session, run: SolveRun) -> None:
    """Project native Worker ledger counts onto the existing Run row.

    A native Worker is represented by one application ToolCall even when its
    sandbox executes multiple internal commands.  This projection is
    idempotent and deliberately runs after the lifecycle finalizer as well as
    before report generation, so legacy ORM commits cannot erase it.
    """

    native_tool_calls = int(
        await session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(
                ToolCall.run_id == run.id,
                ToolCall.execution_layer == "muteki_native",
            )
        )
        or 0
    )
    if not native_tool_calls:
        return
    run.tool_call_count = max(int(run.tool_call_count or 0), native_tool_calls)
    run.run_total_logical_tool_calls = max(
        int(run.run_total_logical_tool_calls or 0), native_tool_calls
    )
    run.attempt_logical_tool_calls = max(
        int(run.attempt_logical_tool_calls or 0), native_tool_calls
    )


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str
    current_phase: str
    error_code: str | None = None
    wp: dict | None = None

    @classmethod
    def from_run(cls, run: SolveRun) -> "RunOutcome":
        checkpoint = run.recovery_checkpoint_json or {}
        return cls(run.id, str(run.status), str(run.current_phase or ""), run.last_error_code, checkpoint.get("wp") or checkpoint.get("current_wp"))


@dataclass(frozen=True)
class MutekiCancelHandoff:
    """Outcome of one Muteki cancel/stop handoff.

    ``forced`` is the only value a caller may turn into a terminal Run without
    the Coordinator's Finalize evidence.  It is always recorded as an explicit
    ``MUTEKI_FINALIZE_GRACE_EXCEEDED_EVENT`` before it is returned, so a forced
    closure can never be silent.
    """

    had_active_task: bool
    task_stopped: bool
    finalize_observed: bool
    forced: bool


def _runs_owning_a_container(run_ids: list[str]) -> list[str] | None:
    """Narrow startup reaping to the Runs that actually still have a container.

    ``teardown_container`` costs two docker processes per call (``docker rm -f``
    plus a legacy per-worker sweep), so asking it about every persisted Run costs
    ``2 * len(run_ids)`` docker invocations on every service start -- even when
    the database holds nothing but Runs that finished months ago and were already
    reaped.  One ``docker ps -a`` answers the same question for all of them.

    Returns ``None`` when the answer cannot be obtained, which tells the caller
    to fall back to reaping everything: an unavailable listing must never be
    mistaken for "no containers exist", or a genuinely orphaned container would
    leak silently.
    """

    try:
        from muteki.solver.container_exec import _docker, _run_container_name, _safe

        listing = _docker("ps", "-a", "--format", "{{.Names}}", timeout=20)
        names = {line.strip() for line in (listing.stdout or "").splitlines() if line.strip()}
    except Exception:
        logger.exception("Cannot list Muteki containers; reaping every terminal Run instead")
        return None
    if not names:
        return []
    # Strays from the old per-worker design are matched by prefix, so collect the
    # candidates once instead of rescanning the whole listing per Run.
    legacy = [name for name in names if name.startswith("muteki-w-")]
    owners: list[str] = []
    for run_id in run_ids:
        try:
            own_name = _run_container_name(run_id)
            own_prefix = f"muteki-w-{_safe(run_id)}-"
        except Exception:
            owners.append(run_id)
            continue
        if own_name in names or any(name.startswith(own_prefix) for name in legacy):
            owners.append(run_id)
    return owners


async def _reap_muteki_container(run_id: str) -> None:
    """Tear down any Sandbox still bearing this Run's name.

    Coordinator Finalize normally owns this, and ``teardown_container`` is
    idempotent, so the ordinary path costs nothing extra.  The reason to ask
    again at the very end of a Muteki session is a container created *after*
    that teardown: measured 2026-10-19, a Run reached COMPLETED_SOLVED at
    16:42:22 while its container had been started one second earlier and then
    sat idle with only ``runtime_agent`` inside -- no owner was left to reap
    it.  This is the last point that still knows the Run's container name.

    The listing is consulted first for the same reason startup reaping does it:
    one ``docker ps -a`` is cheaper than a teardown call per Run.  An unknown
    listing (``None``) falls through to asking, because "could not list" must
    never be read as "nothing is there".
    """

    owners = await asyncio.to_thread(_runs_owning_a_container, [run_id])
    # ``None`` means the listing was unavailable, which must be read as "unknown"
    # rather than "empty" -- an unlistable container is exactly the leak this
    # pass exists to close.
    if owners != []:
        try:
            from muteki.solver.container_exec import teardown_container

            await asyncio.to_thread(teardown_container, run_id, remove=True)
        except Exception:  # noqa: BLE001 - cleanup must not fail the Run
            logger.exception("Muteki container teardown failed run_id=%s", run_id)
    _schedule_late_container_reap(run_id)


# A container can still appear AFTER this pass.  Measured 2026-10-20: Run
# ``eaf1de66`` reached COMPLETED_UNSOLVED at 00:00:45 and its Sandbox was
# started at 00:00:48.7 -- three seconds too late for the teardown above, so it
# idled with no owner left to reap it.  Provisioning can also start from the
# A-lane ``python_run`` channel, which never goes through the official Worker's
# ``_stopped`` guard, so the arrival cannot be prevented at the source.
#
# The window is short and bounded on purpose: two extra ``docker ps -a`` calls
# per Run, and never more than ~30 s of tail.
_LATE_REAP_DELAYS = (8.0, 30.0)
_late_reap_tasks: set[asyncio.Task] = set()


def _schedule_late_container_reap(run_id: str) -> None:
    """Re-check for a Sandbox that is provisioned after the Run went terminal."""

    async def _late_pass() -> None:
        for delay in _LATE_REAP_DELAYS:
            await asyncio.sleep(delay)
            try:
                owners = await asyncio.to_thread(_runs_owning_a_container, [run_id])
            except Exception:  # noqa: BLE001 - a cleanup task must not raise
                logger.exception("Late Muteki container check failed run_id=%s", run_id)
                return
            if not owners:
                # Either it never appeared, or the first pass already removed
                # it.  Keep watching until the window closes either way: "not
                # listed yet" is exactly the state this exists to catch.
                continue
            try:
                from muteki.solver.container_exec import teardown_container

                await asyncio.to_thread(teardown_container, run_id, remove=True)
            except Exception:  # noqa: BLE001 - cleanup must not fail the Run
                logger.exception("Late Muteki container teardown failed run_id=%s", run_id)
            logger.warning(
                "Reaped a Muteki container provisioned after its Run finished run_id=%s",
                run_id,
            )
            return

    try:
        task = asyncio.get_running_loop().create_task(_late_pass())
    except RuntimeError:
        return
    _late_reap_tasks.add(task)
    task.add_done_callback(_late_reap_tasks.discard)
    logger.info("Late Muteki container window opened run_id=%s", run_id)


class RunSupervisor:
    def __init__(self) -> None:
        self.run_supervisor_lock: dict[str, asyncio.Lock] = {}
        # Muteki owns a long-lived Coordinator/Worker task.  Keep the task
        # handle here so the API can cancel the canonical runtime before it
        # terminalizes the outer SolveRun row.  This mirrors Muteki's own
        # Swarm ``finally`` boundary: cancellation must enter the runtime so
        # Coordinator.finalize() and Sandbox teardown can run.
        self._active_run_tasks: dict[str, asyncio.Task] = {}
        self._wake_queue: asyncio.Queue[tuple[str, str, str | None]] | None = None
        self._worker_task: asyncio.Task | None = None
        self._queued_runs: set[str] = set()

    async def start_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._wake_queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="run-supervisor-worker")

    async def stop_worker(self) -> None:
        # The official Muteki Swarm tears down every run-scoped Sandbox from
        # its outer ``finally``.  Mirror that boundary when FastAPI itself is
        # shutting down so a supervisor task cannot outlive the service.  A
        # process shutdown does not write a terminal Run status -- startup
        # reconciliation does -- so it must not wait for (or judge) the
        # Coordinator Finalize handoff.
        for run_id in list(self._active_run_tasks):
            with contextlib.suppress(Exception):
                await self.cancel_muteki(run_id, await_finalize=False)
        task = self._worker_task
        self._worker_task = None
        self._wake_queue = None
        self._queued_runs.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def reap_terminal_muteki_containers(self, runs) -> int:
        """Reap containers left by a previous process after terminalization.

        This is intentionally a startup reconciliation pass, not a periodic
        Docker scan.  The persisted Run status is the durable ownership
        decision; official ``teardown_container`` is idempotent and also
        sweeps old per-worker leftovers for that exact Run ID.  A previous
        process may also have left a RUNNING/PAUSED_RECOVERY Run without a
        live task; those containers are safe to reap because resume recreates
        the run-scoped Sandbox.

        Only Runs that still have a container are handed to ``teardown_container``;
        see ``_runs_owning_a_container`` for why an unconditional per-Run teardown
        makes startup latency proportional to the whole Run history.
        """

        reapable_statuses = {status.value for status in TERMINAL} | {
            RunStatus.RUNNING.value,
            RunStatus.PAUSED_RECOVERY.value,
        }
        terminal_runs = [
            run
            for run in runs
            if str(getattr(run, "solver_mode", "")).lower() == "muteki"
            and str(getattr(run, "status", "")) in reapable_statuses
            and str(getattr(run, "id", "")) not in self._active_run_tasks
        ]
        if not terminal_runs:
            return 0
        try:
            from muteki.solver.container_exec import teardown_container
        except Exception:
            logger.exception("Official Muteki container teardown is unavailable")
            return 0
        run_ids = [str(run.id) for run in terminal_runs]
        owners = await asyncio.to_thread(_runs_owning_a_container, run_ids)
        if owners is None:
            owners = run_ids
        reaped = 0
        for run_id in owners:
            try:
                await asyncio.to_thread(teardown_container, run_id, remove=True)
                reaped += 1
            except Exception:
                logger.exception("Terminal Muteki container reconciliation failed run_id=%s", run_id)
        return reaped

    async def reconcile_muteki_native_terminal(self, session, run: SolveRun) -> RunOutcome | None:
        """Project a durable native Muteki terminal event onto the outer Run.

        Native Muteki can emit ``muteki.run_finished`` and tear down its
        Sandbox before the supervisor commits the outer terminal status.  The
        event stream is the durable handoff: when it is present and no other
        process owns a live lease, close the outer Run from that evidence
        instead of replaying the native runtime.
        """

        if run is None or str(run.solver_mode or "").lower() != "muteki":
            return None
        # An unrecognised persisted status reads as "not terminal": it must stay
        # eligible for reconciliation, and it must not raise -- this call sits in
        # the startup sweep, where one exception used to skip every other Run.
        status = parse_run_status(run.status)
        if status is not None and status in TERMINAL:
            return None
        run_id = str(run.id)
        active_task = self._active_run_tasks.get(run_id)
        current_task = asyncio.current_task()
        if active_task is not None and not active_task.done() and active_task is not current_task:
            return None

        now = datetime.now(UTC)
        lease = await session.scalar(
            select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)
        )
        if lease is not None:
            expires_at = (
                lease.expires_at.replace(tzinfo=UTC)
                if lease.expires_at.tzinfo is None
                else lease.expires_at
            )
            if lease.owner_instance_id != run_attempt_service.owner_instance_id and expires_at > now:
                return None

        event = await session.scalar(
            select(RunEvent)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == MUTEKI_NATIVE_TERMINAL_EVENT,
            )
            .order_by(RunEvent.sequence.desc())
        )
        if event is None:
            return None

        latest_attempt = await session.scalar(
            select(RunAttempt)
            .where(RunAttempt.run_id == run.id)
            .order_by(RunAttempt.attempt_number.desc())
        )
        event_created_at = (
            event.created_at.replace(tzinfo=UTC)
            if event.created_at.tzinfo is None
            else event.created_at
        )
        if latest_attempt is not None and latest_attempt.started_at is not None:
            attempt_started_at = (
                latest_attempt.started_at.replace(tzinfo=UTC)
                if latest_attempt.started_at.tzinfo is None
                else latest_attempt.started_at
            )
            if event_created_at < attempt_started_at:
                return None

        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        flag_found = nested.get("flag_found", payload.get("flag_found"))
        if flag_found is None:
            verified_candidate = await session.scalar(
                select(FlagCandidate.id).where(
                    FlagCandidate.run_id == run.id,
                    FlagCandidate.verified.is_(True),
                )
            )
            flag_found = verified_candidate is not None
        if flag_found is None:
            report = run.report_json if isinstance(run.report_json, dict) else {}
            flag_found = bool(report.get("flag_verified"))
        reason = str(nested.get("reason") or payload.get("reason") or "MUTEKI_NATIVE_FINALIZED")
        if bool(flag_found):
            target = RunStatus.COMPLETED_SOLVED
        elif "TIMEOUT" in reason.upper():
            # A durable terminal event can land after the process is
            # interrupted but before _run_muteki writes the TIMEOUT status.
            # Recovery must preserve the timeout reason instead of projecting
            # the absence of a verified flag as an ordinary unsolved Run.
            target = RunStatus.TIMEOUT
        else:
            target = RunStatus.COMPLETED_UNSOLVED

        current_status = RunStatus(run.status)
        if current_status == RunStatus.RUNNING:
            transition(run, RunStatus.REPORTING)
        elif current_status == RunStatus.REPORTING:
            pass
        elif current_status in {
            RunStatus.PAUSED_RECOVERY,
            RunStatus.PAUSED_CHECKPOINT,
            RunStatus.PAUSED_DEPLOYMENT,
            RunStatus.WAITING_USER,
        }:
            transition(run, RunStatus.PLANNING)
            transition(run, RunStatus.REPORTING)
        else:
            try:
                transition(run, RunStatus.REPORTING)
            except DomainError:
                run.status = RunStatus.REPORTING.value
        transition(run, target)
        run.current_phase = "REPORTING"
        if target == RunStatus.COMPLETED_SOLVED:
            run.last_error_code = "MUTEKI_NATIVE_FINALIZED_RECONCILED_SOLVED"
        elif target == RunStatus.TIMEOUT:
            run.last_error_code = "MUTEKI_NATIVE_FINALIZED_RECONCILED_TIMEOUT"
        else:
            run.last_error_code = "MUTEKI_NATIVE_FINALIZED_RECONCILED_UNSOLVED"
        run.last_error_message = reason[:4000]
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint["muteki_native_terminal"] = {
            "event_id": str(event.id),
            "event_sequence": int(event.sequence),
            "reason": reason,
            "flag_found": bool(flag_found),
            "reconciled_at": now.isoformat(),
            "outer_status": target.value,
        }
        checkpoint["current_phase"] = "REPORTING"
        run.recovery_checkpoint_json = checkpoint
        await event_service.append(
            session,
            run.id,
            MUTEKI_TERMINAL_RECONCILE_EVENT,
            {
                "run_id": run.id,
                "event_id": str(event.id),
                "event_sequence": int(event.sequence),
                "reason": reason,
                "flag_found": bool(flag_found),
                "status": target.value,
            },
        )
        await run_finalizer.reconcile(session, run)
        await session.refresh(run)
        return RunOutcome.from_run(run)

    async def reconcile_muteki_native_terminals(self, session, runs) -> int:
        """Reconcile every persisted native-terminal Muteki Run once.

        Per-Run failures are logged and skipped.  This sweep is the one that
        reclaims leases and reaps containers, so letting one malformed row abort
        it would leave *every* later Run stale -- the blast radius is the whole
        database, which is far worse than one Run going unreconciled.
        """

        reconciled = 0
        for candidate in list(runs):
            run_id = str(getattr(candidate, "id", "") or "")
            if not run_id:
                continue
            try:
                run = await session.get(SolveRun, run_id, populate_existing=True)
                if run is None:
                    continue
                if await self.reconcile_muteki_native_terminal(session, run) is not None:
                    reconciled += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Muteki terminal reconciliation failed run_id=%s", run_id)
        return reconciled

    async def enqueue(self, run_id: str, *, reason: str) -> None:
        await self.start_worker()
        if run_id in self._queued_runs:
            return
        continuation_id: str | None = None
        try:
            async with SessionLocal() as session:
                kind = "USER_INPUT" if reason in {"USER_INPUT_RECEIVED", "USER_INPUT_CONSUMED"} else "CHECKPOINT_RECOVERY"
                current = await session.get(SolveRun, run_id)
                revision = int(current.context_revision or 0) if current is not None else 0
                phase = str(current.current_phase or "") if current is not None else ""
                item = await continuation_service.request(
                    session,
                    run_id,
                    kind=kind,
                    dedupe_key=f"{kind}:{run_id}:{revision}:{phase}",
                    payload={"reason": reason, "context_revision": revision, "phase": phase},
                )
                continuation_id = str(item.id)
                await session.commit()
        except Exception:
            # Compatibility during migration rollout and for isolated tests.
            logger.exception("Continuation persistence unavailable run_id=%s reason=%s", run_id, reason)
        self._queued_runs.add(run_id)
        assert self._wake_queue is not None
        await self._wake_queue.put((run_id, reason, continuation_id))

    async def enqueue_continuation(self, continuation_id: str, run_id: str) -> None:
        await self.start_worker()
        if run_id in self._queued_runs:
            return
        self._queued_runs.add(run_id)
        assert self._wake_queue is not None
        await self._wake_queue.put((run_id, "PERSISTED_CONTINUATION", continuation_id))

    async def recover_pending_continuations(self) -> int:
        async with SessionLocal() as session:
            await continuation_service.recover_stale(session)
            rows = await continuation_service.pending(session)
        for item in rows:
            await self.enqueue_continuation(item["id"], item["run_id"])
        return len(rows)

    async def _execute_continuation(self, continuation_id: str) -> None:
        # Claim with a short-lived session, then cross the runtime boundary
        # using only scalar IDs.  Keeping the claimed ORM row/session alive
        # while entering orchestrator.start() allows an expired relationship
        # to trigger an implicit async lazy load (MissingGreenlet).
        continuation = None
        async with SessionLocal() as session:
            item = await continuation_service.claim(session, continuation_id)
            if item is None:
                return
            payload = dict(item.get("payload") or {})
            continuation = {
                "id": str(item["id"]),
                "kind": str(item["kind"]),
                "run_id": str(item["run_id"]),
                "producing_task_id": str(payload.get("producing_task_id") or ""),
                "user_message": payload.get("user_message"),
            }
        # Promotion continuations are only a durable wake-up signal. They
        # must not remain RUNNING while the next controller cycle evaluates
        # the Planner barrier, otherwise the continuation blocks the Planner
        # that it is responsible for waking.
        completed_before_runtime = continuation["kind"] == "RESULT_REVIEW_PROMOTION"
        if completed_before_runtime:
            async with SessionLocal() as session:
                await continuation_service.complete(session, continuation["id"])
        try:
            if continuation["kind"] == "RESULT_REVIEW_PENDING":
                from app.orchestration.multi_agent_orchestrator import multi_agent_orchestrator

                await multi_agent_orchestrator._resume_result_review(
                    continuation["producing_task_id"]
                )
            elif continuation["kind"] == "USER_INPUT":
                async with SessionLocal() as session:
                    await self.continue_after_user_input(session, continuation["run_id"])
            else:
                # Route through this supervisor's own dispatcher instead of
                # calling the legacy controller directly.
                #
                # A persisted CHECKPOINT_RECOVERY continuation is what a service
                # restart produces for a Run it interrupted -- and that Run may
                # well be a canonical Muteki one, since a restart in the middle
                # of a solve is exactly when one is written.  Entering
                # ``orchestrator.start`` for it built the *legacy* engine and
                # died on ``unsupported engine type: claude_cli``: an error that
                # names a CLI but is really "this Run belongs to another
                # controller".  ``continue_until_terminal`` routes by
                # ``solver_mode`` and is the same entry a normal start uses, so
                # there is one dispatch table rather than two that can diverge.
                async with SessionLocal() as session:
                    await self.continue_until_terminal(
                        session, continuation["run_id"], continuation["user_message"]
                    )
            if not completed_before_runtime:
                async with SessionLocal() as session:
                    await continuation_service.complete(session, continuation["id"])
        except Exception as error:
            logger.exception("Durable continuation failed id=%s run_id=%s", continuation["id"], continuation["run_id"])
            if not completed_before_runtime:
                async with SessionLocal() as session:
                    await continuation_service.fail(session, continuation["id"], error)

    def _release_run_lock(self, run_id: str, lock: asyncio.Lock) -> None:
        lock.release()
        if not lock.locked() and self.run_supervisor_lock.get(run_id) is lock:
            self.run_supervisor_lock.pop(run_id, None)

    def _register_current_run_task(self, run_id: str) -> asyncio.Task | None:
        task = asyncio.current_task()
        if task is not None:
            self._active_run_tasks[run_id] = task
        return task

    def _unregister_run_task(self, run_id: str, task: asyncio.Task | None) -> None:
        if task is not None and self._active_run_tasks.get(run_id) is task:
            self._active_run_tasks.pop(run_id, None)

    async def cancel_muteki_with_handoff(
        self,
        run_id: str,
        *,
        grace_seconds: float = MUTEKI_CANCEL_GRACE_SECONDS,
        finalize_grace_seconds: float = MUTEKI_FINALIZE_GRACE_SECONDS,
        await_finalize: bool = True,
    ) -> MutekiCancelHandoff:
        """Cancel one canonical Muteki run and hand off its Finalize safely.

        The outer API must not mark a native Run terminal while its
        Coordinator is still able to append Graph events.  Cancelling the
        supervisor task first enters the official Swarm/Worker ``finally``
        paths, but a cancelled task can also end *while* ``Coordinator.finalize``
        (RUN_FINISHED, Worker cancellation, Sandbox teardown) is still running
        -- for example when the cancellation lands inside the Runtime's own
        cooperative total-timeout wait.  This method therefore waits twice:
        once for the cancelled task, then for the Coordinator's durable
        ``run_finished`` handoff.  Only when both windows expire is the closure
        forced, and that forced closure is recorded as explicit evidence
        instead of being written silently.

        The idempotent container teardown below also covers an orphan left by a
        process restart where this supervisor has no task handle.
        """

        task = self._active_run_tasks.get(run_id)
        current = asyncio.current_task()
        had_active_task = task is not None and task is not current
        task_stopped = True
        finalize_observed = True
        forced = False
        if had_active_task:
            task.cancel()
            task_stopped = await self._await_muteki_task(task, grace_seconds)
            if not task_stopped:
                logger.warning(
                    "Muteki task did not stop within grace run_id=%s grace_seconds=%.1f",
                    run_id,
                    grace_seconds,
                )
            if await_finalize:
                # ``_run_muteki`` releases the attempt/lease and the outer Run
                # is closed by the caller right after this method returns, so
                # the Coordinator's terminal handoff has to land first.
                finalize_observed = await self._await_muteki_finalize_handoff(
                    run_id, finalize_grace_seconds
                )
                if not finalize_observed:
                    forced = True
                    await self._record_muteki_finalize_grace_exceeded(
                        run_id,
                        reason="CANCEL_FINALIZE_GRACE_EXCEEDED",
                        detail={
                            "grace_seconds": float(grace_seconds),
                            "finalize_grace_seconds": float(finalize_grace_seconds),
                            "task_stopped": bool(task_stopped),
                        },
                    )
                    task.cancel()
                elif not task_stopped:
                    task_stopped = await self._await_muteki_task(
                        task, MUTEKI_FINALIZE_SETTLE_SECONDS
                    )

        # Official Muteki teardown is idempotent and is the only container
        # cleanup authority.  It is also required for orphaned tasks from a
        # previous API process which cannot be present in this registry.
        try:
            from muteki.solver.container_exec import teardown_container

            await asyncio.to_thread(teardown_container, run_id, remove=True)
        except Exception:
            logger.exception("Muteki container teardown failed run_id=%s", run_id)
        return MutekiCancelHandoff(
            had_active_task=had_active_task,
            task_stopped=task_stopped,
            finalize_observed=finalize_observed,
            forced=forced,
        )

    async def cancel_muteki(
        self,
        run_id: str,
        *,
        grace_seconds: float = MUTEKI_CANCEL_GRACE_SECONDS,
        finalize_grace_seconds: float = MUTEKI_FINALIZE_GRACE_SECONDS,
        await_finalize: bool = True,
    ) -> bool:
        """Cancel and reap one canonical Muteki run before lifecycle close.

        Returns whether this supervisor owned a live task for the Run.  See
        :meth:`cancel_muteki_with_handoff` for the Finalize-handoff contract.
        """

        handoff = await self.cancel_muteki_with_handoff(
            run_id,
            grace_seconds=grace_seconds,
            finalize_grace_seconds=finalize_grace_seconds,
            await_finalize=await_finalize,
        )
        return handoff.had_active_task

    async def _await_muteki_task(self, task: asyncio.Task, timeout_seconds: float) -> bool:
        """Wait for one cancelled Muteki task to finish its ``finally`` blocks."""

        if task.done():
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=max(0.1, float(timeout_seconds)),
            )
        except asyncio.CancelledError:
            # The cancelled Muteki task is expected to propagate
            # CancelledError after its finally blocks have completed.
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:
            logger.exception("Muteki task failed while being cancelled")
            return True
        return True

    async def _muteki_finalize_handoff_state(self, run_id: str) -> str:
        """Classify the durable Coordinator handoff for one Run.

        ``pending`` means a native Muteki session already produced Graph events
        but has not written its terminal ``run_finished`` handoff yet.  Every
        other answer (``observed``/``none``/``unknown``) keeps the caller's
        previous behaviour, so an unreachable database can never turn a
        cancellation into an indefinite wait.
        """

        try:
            async with SessionLocal() as session:
                run = await session.get(SolveRun, run_id)
                if run is None:
                    return "unknown"
                terminal = await session.scalar(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == MUTEKI_NATIVE_TERMINAL_EVENT,
                    )
                    .order_by(RunEvent.sequence.desc())
                )
                if terminal is not None:
                    attempt = await session.scalar(
                        select(RunAttempt)
                        .where(RunAttempt.run_id == run_id)
                        .order_by(RunAttempt.attempt_number.desc())
                    )
                    if attempt is not None and attempt.started_at is not None:
                        event_at = terminal.created_at
                        attempt_at = attempt.started_at
                        if event_at.tzinfo is None:
                            event_at = event_at.replace(tzinfo=UTC)
                        if attempt_at.tzinfo is None:
                            attempt_at = attempt_at.replace(tzinfo=UTC)
                        if event_at < attempt_at:
                            # A handoff from an earlier attempt is not evidence
                            # for the attempt that is running right now.
                            return "pending"
                    return "observed"
                activity = await session.scalar(
                    select(RunEvent.id)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type.like("muteki.%"),
                    )
                    .limit(1)
                )
                return "pending" if activity is not None else "none"
        except Exception:
            logger.debug(
                "Muteki Finalize handoff state unavailable run_id=%s", run_id, exc_info=True
            )
            return "unknown"

    async def _await_muteki_finalize_handoff(self, run_id: str, timeout_seconds: float) -> bool:
        """Wait for the Coordinator's durable RUN_FINISHED handoff.

        Returns ``True`` as soon as the handoff is observed (or cannot be
        observed at all) and ``False`` only when a live native session has still
        not produced it when the window expires.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_seconds))
        while True:
            state = await self._muteki_finalize_handoff_state(run_id)
            if state != "pending":
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, max(0.05, remaining)))

    async def _record_muteki_finalize_grace_exceeded(
        self,
        run_id: str,
        *,
        reason: str,
        detail: dict | None = None,
    ) -> None:
        """Record a Muteki closure that happened without the Finalize handoff.

        Forced closure is a lifecycle anomaly, never a normal path, so it is
        always logged at ``error`` level and persisted as a Run event that
        carries the exact reason and window.
        """

        payload = {"run_id": run_id, "reason": reason, **(detail or {})}
        logger.error(
            "Muteki Run closed without the Coordinator Finalize handoff run_id=%s "
            "reason=%s detail=%s",
            run_id,
            reason,
            detail or {},
        )
        try:
            async with SessionLocal() as session:
                await event_service.append(
                    session,
                    run_id,
                    MUTEKI_FINALIZE_GRACE_EXCEEDED_EVENT,
                    payload,
                )
        except Exception:
            # The durable record is best effort: a cleared database or a
            # shutdown race must not block the forced teardown itself.
            logger.exception(
                "Muteki Finalize handoff evidence could not be persisted run_id=%s", run_id
            )

    async def _worker_loop(self) -> None:
        # Keep the queue object local for the lifetime of this loop.  Shutdown
        # clears ``self._wake_queue`` before cancelling the task; using the
        # attribute in ``finally`` then loses the queue item accounting and
        # masks the original cancellation with AttributeError.
        queue = self._wake_queue
        assert queue is not None
        while True:
            run_id, reason, continuation_id = await queue.get()
            self._queued_runs.discard(run_id)
            try:
                if continuation_id is not None:
                    await self._execute_continuation(continuation_id)
                elif reason in {"USER_INPUT_RECEIVED", "USER_INPUT_CONSUMED"}:
                    task = asyncio.create_task(
                        self.run_after_user_input_background(run_id),
                        name=f"muteki-user-input-run-{run_id}",
                    )
                    try:
                        await task
                    except asyncio.CancelledError:
                        # Cancelling the child Run must not terminate the
                        # supervisor queue.  A cancellation of the queue
                        # itself still propagates during service shutdown.
                        if asyncio.current_task() and asyncio.current_task().cancelling():
                            raise
                else:
                    task = asyncio.create_task(
                        self.run_background(run_id),
                        name=f"muteki-run-{run_id}",
                    )
                    try:
                        await task
                    except asyncio.CancelledError:
                        if asyncio.current_task() and asyncio.current_task().cancelling():
                            raise
            except (Exception, SystemExit):
                # Last-resort net for the whole supervisor queue.  ``SystemExit``
                # is included because it is a ``BaseException``: a plain
                # ``except Exception`` would let one escape, and an escaping
                # ``SystemExit`` in the loop thread tears down the event loop --
                # one bad Run ending the whole service.  A wake-up failing is
                # normal; the queue must survive it and take the next one.
                logger.exception("Supervisor wakeup failed run_id=%s reason=%s", run_id, reason)
            finally:
                queue.task_done()

    def _asset_mysql(self, challenge: Challenge | None) -> bool:
        metadata = (challenge.metadata_json or {}) if challenge else {}
        return str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql"

    async def _facts(self, session, run: SolveRun) -> tuple[set[str], dict, bool, set[str]]:
        keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
            VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"
        ))).all())
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = dict(state.capability_ledger_json or {}) if state else {}
        candidate = bool(await session.scalar(select(FlagCandidate.id).where(
            FlagCandidate.run_id == run.id,
            FlagCandidate.verified.is_(False),
            FlagCandidate.source_artifact_id.is_not(None),
            FlagCandidate.source_tool_call_id.is_not(None),
        )))
        calls = list((await session.scalars(select(ToolCall).where(
            ToolCall.run_id == run.id, ToolCall.tool_name == "sql_boolean_compare", ToolCall.status == "COMPLETED"
        ))).all())
        tested = {str((call.arguments_json or {}).get("test_field")) for call in calls if isinstance(call.arguments_json, dict) and (call.arguments_json or {}).get("test_field")}
        return keys, ledger, candidate, tested

    async def _progress_snapshot(self, session, run_id: str) -> dict[str, int]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
        capability_count = len((state.capability_ledger_json or {}) if state else {})
        return {
            "task_count": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run_id)) or 0),
            "proposal_count": int(await session.scalar(select(func.count()).select_from(PlannerProposal).where(PlannerProposal.run_id == run_id)) or 0),
            "tool_call_count": int(await session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)) or 0),
            "verified_fact_count": int(await session.scalar(select(func.count()).select_from(VerifiedFact).where(VerifiedFact.run_id == run_id, VerifiedFact.promotion_status == "VERIFIED")) or 0),
            "capability_count": capability_count,
            # Tool-manifest refresh is bootstrap bookkeeping, not a post-input
            # execution decision. Exclude it from the progress signal so a
            # manifest-only attempt cannot satisfy the resume watchdog.
            "event_sequence": int(await session.scalar(select(func.max(RunEvent.sequence)).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type != "attempt.tool_manifest_refreshed",
            )) or 0),
        }

    async def has_unfinished_user_input(self, session, run: SolveRun) -> bool:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        if checkpoint.get("user_input_resume_pending"):
            return True
        last_input = await session.scalar(select(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "CONSUMED",
        ).order_by(RunUserInput.consumed_at.desc(), RunUserInput.created_at.desc()))
        if last_input is None or last_input.consumed_at is None:
            return False
        last_event = await session.scalar(select(RunEvent).where(
            RunEvent.run_id == run.id,
        ).order_by(RunEvent.event_id.desc(), RunEvent.sequence.desc()))
        return bool(last_event and last_event.event_type in {"user_input.consumed", "user.input_consumed"})

    async def _recover_consumed_user_input(self, session, run: SolveRun) -> dict:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        ids = list(checkpoint.get("last_user_input_ids") or [])
        query = select(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "CONSUMED",
        )
        if ids:
            query = query.where(RunUserInput.id.in_(ids))
        rows = list((await session.scalars(query.order_by(RunUserInput.revision, RunUserInput.created_at))).all())
        if not rows:
            return {"items": [], "text": ""}
        return {
            "items": rows,
            "text": "\n\n".join(f"User supplemental input v{item.revision}: {item.content}" for item in rows),
        }

    async def _mark_user_input_no_progress(self, session, run: SolveRun, snapshot: dict) -> RunOutcome:
        run.last_error_code = "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH"
        run.last_error_message = "User input was consumed, but the multi-agent supervisor did not create a post-input execution decision within 30 seconds."
        run.current_phase = "WAITING_USER"
        run.recovery_checkpoint_json = {
            **(run.recovery_checkpoint_json or {}),
            "current_phase": "WAITING_USER",
            "no_progress_reason": "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH",
            "user_input_resume_pending": True,
            "progress_snapshot": snapshot,
            "question": "The input was accepted, but no Planner/Task/Tool execution path was created. Choose how to continue.",
            "options": ["retry", "finish_unsolved_wp", "try_alternative_strategy"],
        }
        run.status = RunStatus.WAITING_USER.value
        await event_service.append(session, run.id, "supervisor.no_progress", {
            "run_id": run.id,
            "reason": "USER_INPUT_NO_PROGRESS",
            "progress_snapshot": snapshot,
        })
        return await self._outcome(session, run)

    async def _set_stage(self, session, run: SolveRun, stage: str) -> None:
        run.current_phase = stage
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint["current_phase"] = stage
        checkpoint["checkpoint_type"] = f"{stage}_ACTIVE"
        run.recovery_checkpoint_json = checkpoint
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if state:
            state.current_phase = stage
            state.run_plan_json = {**(state.run_plan_json or {}), "current_phase": stage}
        await session.commit()

    async def _recover_internal_pause(self, session, run: SolveRun, stage: str) -> None:
        if str(run.status) not in {"PAUSED_RECOVERY", "PAUSED_CHECKPOINT"}:
            return
        if run.last_error_code in {"MYSQL_PREDICATE_NOT_CONFIRMED", "MYSQL_METADATA_STAGE_EMPTY_RESULT"}:
            return
        await self._set_stage(session, run, stage)
        run.last_error_code = None
        run.last_error_message = None
        try:
            transition(run, RunStatus.PLANNING)
        except Exception:
            run.status = RunStatus.PLANNING.value
        await session.commit()

    async def _outcome(self, session, run: SolveRun) -> RunOutcome:
        if str(run.status) == "WAITING_USER":
            checkpoint = dict(run.recovery_checkpoint_json or {})
            resume_broken = checkpoint.get("user_input_resume_pending") and run.last_error_code in {
                "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH",
                "USER_INPUT_NO_PROGRESS",
            }
            if not resume_broken and not checkpoint.get("current_wp"):
                checkpoint["current_wp"] = await writeup_builder.build_partial_wp(
                    session, run, str(run.last_error_code or "Waiting for user input.")
                )
                run.recovery_checkpoint_json = checkpoint
                run.report_json = checkpoint["current_wp"]
                await session.commit()
        return RunOutcome.from_run(run)

    async def _resolve_waiting_input(self, session, run: SolveRun) -> None:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint.pop("question", None)
        checkpoint.pop("options", None)
        checkpoint["waiting_resolved_at"] = datetime.now(UTC).isoformat()
        run.recovery_checkpoint_json = checkpoint
        run.last_error_code = None
        run.last_error_message = None
        try:
            transition(run, RunStatus.PLANNING)
        except Exception:
            run.status = RunStatus.PLANNING.value
        await session.commit()

    async def continue_after_user_input(self, session, run_id: str) -> RunOutcome:
        run = await session.get(SolveRun, run_id)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        if str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
            return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_ALREADY_TERMINAL")
        if str(run.status) not in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
            return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_NOT_WAITING")
        # Establish the resume execution context before consuming input so
        # user_input.consumed carries an attempt_id. The orchestrator reuses
        # this lease instead of bootstrapping a manifest-only attempt.
        await run_attempt_service.reclaim_expired_lease(session, run.id)
        pending_id = await session.scalar(select(RunUserInput.id).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "QUEUED",
            RunUserInput.consumed_at.is_(None),
        ).order_by(RunUserInput.revision, RunUserInput.created_at))
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)) if pending_id else None
        attempt = await session.get(RunAttempt, lease.attempt_id) if lease is not None else None
        if pending_id and attempt is None:
            attempt, lease = await run_attempt_service.begin(session, run)
        consumed = await consume_user_inputs(session, run, attempt, wake_supervisor=False)
        if not consumed["items"]:
            consumed = await self._recover_consumed_user_input(session, run)
            if not consumed["items"]:
                return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "NO_PENDING_USER_INPUT")
            checkpoint = dict(run.recovery_checkpoint_json or {})
            checkpoint["user_input_resume_pending"] = True
            run.recovery_checkpoint_json = checkpoint
            await session.commit()
        if str(run.status) in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
            await self._resolve_waiting_input(session, run)
        # Make the resume context explicit for the next Planner snapshot.
        checkpoint = dict(run.recovery_checkpoint_json or {})
        progress = dict((await self._facts(session, run))[1].get("metadata_progress") or {})
        blocked = [f"MYSQL_METADATA_DISCOVERY.{stage}" for stage, item in progress.items() if isinstance(item, dict) and str(item.get("status") or "").upper() == "BLOCKED"]
        checkpoint.update({
            "resume_reason": "USER_INPUT_RECEIVED",
            "blocked_stage": blocked[-1] if blocked else None,
            "suggested_strategy": "try next unblocked metadata stage or alternative bounded extraction",
            "planner_context": {
                "user_inputs": list((run.hints_json or {}).get("user_inputs") or [])[-20:],
                "resume_reason": "USER_INPUT_RECEIVED",
                "blocked_stage": blocked[-1] if blocked else None,
                "metadata_progress": progress,
            },
        })
        run.recovery_checkpoint_json = checkpoint
        await session.commit()
        try:
            outcome = await self.continue_until_terminal(session, run_id, consumed["text"])
            run = await session.get(SolveRun, run_id)
            if run is not None:
                checkpoint = dict(run.recovery_checkpoint_json or {})
                checkpoint["user_input_resume_pending"] = False
                run.recovery_checkpoint_json = checkpoint
                await session.commit()
            return outcome
        except Exception:
            # Keep the durable pending marker so the watchdog can retry after
            # a process crash or a transient infrastructure failure.
            raise

    async def continue_until_terminal(self, session, run_id: str, user_message: str | None = None) -> RunOutcome:
        # ``solver_v2`` is an explicit opt-in execution mode.  Keep the
        # legacy supervisor/orchestrator path compatible for existing runs
        # and route only the new mode to SolverRuntimeService.
        selected = await session.get(SolveRun, run_id)
        if selected is not None and selected.solver_mode == "muteki":
            reconciled = await self.reconcile_muteki_native_terminal(session, selected)
            if reconciled is not None:
                return reconciled
            selected = await session.get(SolveRun, run_id, populate_existing=True)
            if selected is not None and RunStatus(selected.status) in TERMINAL:
                return await self._outcome(session, selected)
            if selected is not None:
                return await self._run_muteki(session, selected)
        if selected is not None and selected.solver_mode == "solver_v2":
            from app.solver.service import solver_runtime_service

            return await solver_runtime_service.run(session, run_id, user_message)

        from app.orchestration.orchestrator import orchestrator

        for _ in range(32):
            await session.rollback()
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            await run_finalizer.reconcile(session, run)
            await session.refresh(run)
            if str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                return await self._outcome(session, run)
            active_continuation = await session.scalar(select(RunContinuation).where(
                RunContinuation.run_id == run.id,
                RunContinuation.status.in_(
                    ("PENDING", "RUNNING")
                ),
            ).order_by(RunContinuation.created_at.desc()))
            if active_continuation is not None:
                # A persisted continuation owns the next runtime boundary.
                # Do not enter orchestrator.start from the periodic recovery
                # driver while that ID-scoped continuation is still active.
                return RunOutcome(
                    run.id,
                    str(run.status),
                    str(run.current_phase or ""),
                    "CONTINUATION_ACTIVE",
                )
            consumed = await consume_user_inputs(session, run, wake_supervisor=False)
            if consumed["items"] and str(run.status) in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
                await self._resolve_waiting_input(session, run)
                run = await session.get(SolveRun, run_id)
            if str(run.status) == "WAITING_USER":
                return await self._outcome(session, run)
            active_lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
            # A completed Planner task without its proposal/review/action
            # chain is a controller persistence break, not ordinary lack of
            # tool progress. Never convert it directly into a WP.
            last_planner = await session.scalar(select(AgentTask).where(
                AgentTask.run_id == run.id,
                AgentTask.agent_role == "PLANNER",
                AgentTask.status == "COMPLETED",
            ).order_by(AgentTask.updated_at.desc()))
            if last_planner is not None and active_lease is None:
                proposal_for_planner = await session.scalar(select(PlannerProposal).where(PlannerProposal.created_by_task_id == last_planner.id))
                if proposal_for_planner is None:
                    run.status = RunStatus.WAITING_USER.value
                    run.current_phase = "WAITING_USER"
                    run.last_error_code = "PLANNER_RESULT_NOT_DISPATCHED"
                    run.last_error_message = "Planner completed but no proposal/review/action was dispatched."
                    run.recovery_checkpoint_json = {
                        **(run.recovery_checkpoint_json or {}),
                        "classification": "PLANNER_RESULT_NOT_DISPATCHED",
                        "last_planner_task_id": last_planner.id,
                        "reason": "planner completed but no proposal/review/action was dispatched",
                        "expected_next": "persist planner proposal and dispatch PLAN_REVIEW",
                        "safe_retry": True,
                    }
                    await session.commit()
                    return await self._outcome(session, run)
            if active_lease is not None and run_id not in orchestrator.active_tasks and active_lease.owner_instance_id != run_attempt_service.owner_instance_id:
                return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_ALREADY_OWNED")
            counters = dict((run.recovery_checkpoint_json or {}).get("supervisor_counters") or {})
            if active_lease is None and int(counters.get("no_progress_count") or 0) >= 1 and (run.recovery_checkpoint_json or {}).get("resume_reason") != "USER_INPUT_RECEIVED":
                await run_finalizer.finish_unsolved_with_wp(session, run, "NO_PROGRESS_LOOP")
                return await self._outcome(session, run)
            challenge = await session.get(Challenge, run.challenge_id)
            before_keys, before_ledger, candidate, tested = await self._facts(session, run)
            before_snapshot = await self._progress_snapshot(session, run_id)
            metadata = (challenge.metadata_json or {}) if challenge else {}
            declared_fields = {str(item) for item in (metadata.get("fields") or []) if str(item)}
            decision = stage_decider.decide(
                asset_warranty_mysql=self._asset_mysql(challenge),
                verified_fact_keys=before_keys,
                capability_ledger=before_ledger,
                candidate_exists=candidate,
                declared_fields=declared_fields,
                tested_fields=tested,
            )
            if decision.terminal_reason:
                run.last_error_code = decision.terminal_reason
                run.recovery_checkpoint_json = {**(run.recovery_checkpoint_json or {}), "classification": decision.terminal_reason, **decision.details}
                await run_finalizer.finish_unsolved_with_wp(session, run, decision.reason)
                return RunOutcome.from_run(run)
            if decision.requires_user:
                run.status = RunStatus.WAITING_USER.value
                run.current_phase = "WAITING_USER"
                run.last_error_code = "METADATA_ESSENTIAL_STAGES_BLOCKED"
                run.last_error_message = decision.reason
                run.recovery_checkpoint_json = {
                    **(run.recovery_checkpoint_json or {}),
                    "current_phase": "WAITING_USER",
                    "question": decision.reason,
                    "options": ["retry_after_fix", "finish_unsolved_wp", "try_alternative_strategy"],
                    **decision.details,
                }
                await session.commit()
                return await self._outcome(session, run)
            await self._recover_internal_pause(session, run, decision.stage)
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if str(run.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, run)
            await self._set_stage(session, run, decision.stage)
            next_message = consumed["text"] or (user_message if user_message and _ == 0 else None)
            checkpoint = dict(run.recovery_checkpoint_json or {})
            checkpoint["planner_context_consumed"] = True
            run.recovery_checkpoint_json = checkpoint
            await session.commit()
            active_production_calls = await execution_recovery_guard.active_production_calls(
                session, run.id
            )
            if active_production_calls:
                return RunOutcome(
                    run.id,
                    str(run.status),
                    str(run.current_phase or ""),
                    "PRODUCTION_EXECUTION_ACTIVE",
                )
            await orchestrator.start(run_id, next_message)
            user_message = None
            await session.rollback()
            after = await session.get(SolveRun, run_id)
            if after is None:
                raise ValueError("RUN_NOT_FOUND")
            after_keys, after_ledger, after_candidate, _ = await self._facts(session, after)
            after_snapshot = await self._progress_snapshot(session, run_id)
            observed = supervisor_progress_evaluator.observe(
                after.recovery_checkpoint_json or {}, stage=decision.stage,
                error_code=after.last_error_code,
                before_facts=before_keys, after_facts=after_keys,
                before_capabilities=set(before_ledger), after_capabilities=set(after_ledger),
                candidate_exists=after_candidate,
                progress_snapshot_changed=before_snapshot != after_snapshot,
            )
            after.recovery_checkpoint_json = {
                **(after.recovery_checkpoint_json or {}),
                "progress_snapshot": after_snapshot,
            }
            if after_snapshot != before_snapshot:
                after.recovery_checkpoint_json.pop("resume_reason", None)
            await session.commit()
            if str(after.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, after)
            if observed.needs_user:
                after.status = RunStatus.WAITING_USER.value
                after.current_phase = "WAITING_USER"
                after.recovery_checkpoint_json = {**(after.recovery_checkpoint_json or {}), "question": observed.reason, "options": ["retry", "finish_unsolved_wp"], "current_phase": "WAITING_USER"}
                await session.commit()
                return await self._outcome(session, after)
            if observed.terminal_unsolved:
                if (after.recovery_checkpoint_json or {}).get("resume_reason") == "USER_INPUT_RECEIVED" and observed.reason == "NO_PROGRESS_LOOP":
                    return await self._mark_user_input_no_progress(session, after, after_snapshot)
                await run_finalizer.finish_unsolved_with_wp(session, after, observed.reason)
                return await self._outcome(session, after)
        run = await session.get(SolveRun, run_id)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        if (run.recovery_checkpoint_json or {}).get("resume_reason") == "USER_INPUT_RECEIVED":
            return await self._mark_user_input_no_progress(session, run, {"reason": "NO_PROGRESS_LOOP"})
        await run_finalizer.finish_unsolved_with_wp(session, run, "NO_PROGRESS_LOOP")
        return await self._outcome(session, run)

    async def _record_runtime_flag_isolated(
        self, run_id: str, candidate: str, *, required: bool = True
    ) -> None:
        """Write a reported flag to the ledger on a session of its own.

        ``_run_muteki`` shares its session with ``MutekiRuntime``, whose Workers
        touch the event loop's session from ``asyncio.to_thread`` callbacks.
        By the time a solved Run reports its flag that session can already be in
        a failed-flush state, and every ORM read on it then raises
        ``PendingRollbackError`` or ``MissingGreenlet``.  The old code caught
        that, logged a warning and carried on -- so Run ``bc80a2fd`` reached
        ``COMPLETED_SOLVED`` holding the byte-exact flag while
        ``flag_candidates`` stayed empty.  The benchmark evaluator reads only
        that table, so the Run was a real solve scored as INCOMPLETE.

        A fresh session has none of that history: it loads the Run and its
        Challenge by primary key and commits on its own, so the ledger cannot be
        lost to whatever happened to the solve transaction.
        """

        try:
            async with SessionLocal() as ledger:
                fresh_run = await ledger.get(SolveRun, run_id)
                if fresh_run is None:
                    raise LookupError(f"run {run_id} disappeared before its flag was recorded")
                fresh_challenge = await ledger.get(Challenge, fresh_run.challenge_id)
                if fresh_challenge is None:
                    raise LookupError(
                        f"challenge {fresh_run.challenge_id} missing for run {run_id}"
                    )
                await flag_service.record_runtime_flag(
                    ledger, fresh_run, fresh_challenge, candidate
                )
        except Exception:  # noqa: BLE001 - see the docstring for why this is contained
            if not required:
                logger.warning(
                    "unable to record suspected flag for run %s", run_id, exc_info=True
                )
                return
            # A real flag that cannot reach the ledger turns a solve into an
            # INCOMPLETE in every downstream report.  Log at error level and
            # leave a durable Run event, so this is never silent again.
            logger.error(
                "flag ledger write FAILED for run %s; the solve will read as INCOMPLETE",
                run_id,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                async with SessionLocal() as ledger:
                    await event_service.append(
                        ledger,
                        run_id=run_id,
                        event_type="flag.ledger_write_failed",
                        payload={"candidate": candidate, "reason": "see backend log"},
                    )
                    await ledger.commit()

    async def _run_muteki(self, session, run: SolveRun) -> RunOutcome:
        """Run the canonical Muteki engine as an explicit opt-in mode."""
        from app.models.challenge import Challenge
        from app.orchestration.state_machine import RunStatus, transition
        from app.solver.muteki.runtime import MutekiRuntime

        run_id = str(run.id)
        challenge = await session.get(Challenge, run.challenge_id)
        if challenge is None:
            raise ValueError("CHALLENGE_NOT_FOUND")
        if str(run.status) == RunStatus.CREATED.value:
            transition(run, RunStatus.RUNNING)
            await session.commit()
        attempt = lease = None
        attempt_id = lease_id = None
        heartbeat_task: asyncio.Task | None = None
        try:
            attempt, lease = await run_attempt_service.begin(session, run)
            attempt_id = str(attempt.id) if attempt is not None else None
            lease_id = str(lease.id) if lease is not None else None
            # The official Muteki Worker emits a 15-second liveness heartbeat
            # while its long-lived Sandbox/CLI turn is running.  The outer
            # application lease must receive the same signal through an
            # independent DB session; the runtime owns the session passed to
            # Muteki and may spend many minutes inside one Worker turn.
            heartbeat_task = asyncio.create_task(
                self._heartbeat_muteki_attempt(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                ),
                name=f"muteki-attempt-heartbeat-{run_id}",
            )
            # Canonical Muteki native Workers use their own official Worker
            # image/SharedGraph boundary and do not execute through the legacy
            # Runner tool catalog.  Applying the legacy manifest gate here can
            # pause a valid native run because optional legacy tools (for
            # example sqlmap_detect) are absent even though Muteki never needs
            # them.  Solver v2/legacy paths keep their existing manifest gate.
            await session.commit()
            result = await MutekiRuntime(session, run, challenge).run_once(
                # Muteki's Coordinator is graph/event driven.  The persisted
                # Agent-step limit belongs to Worker/CLI turns and must not
                # truncate the outer OODA loop after a few tool intents.  The
                # Run-level total-runtime limit remains the safety boundary.
                max_rounds=None,
                max_workers=10,
            )
            try:
                await session.refresh(run)
            except Exception:  # noqa: BLE001 - a broken session must not kill the Run
                # ``run_once`` drives container work from Worker threads.  When
                # one of those touches the async session, the ORM comes back
                # with expired attributes and no greenlet left to reload them;
                # the refresh then raises ``MissingGreenlet`` and, before this
                # guard, took the whole lifecycle coroutine with it -- leaving
                # a finished Run parked on RUNNING forever.  Re-opening the row
                # by primary key is enough to continue.
                logger.warning(
                    "Muteki refresh failed run_id=%s; reloading the Run row",
                    run_id,
                    exc_info=True,
                )
                await session.rollback()
                run = await session.get(SolveRun, run_id, populate_existing=True)
                if run is None:
                    raise
            # Native Muteki Workers are one durable application ToolCall even
            # though their internal CLI may issue several sandbox commands.
            # Reconcile the existing Run counters from that authoritative
            # ledger before report generation and attempt finalization.
            await _reconcile_muteki_run_counters(session, run)
            if result.status == RunStatus.COMPLETED_SOLVED.value:
                # The canonical graph owns Prepare/Race/Coordinator/Finalize
                # phases.  Persist the outer lifecycle's terminal reporting
                # phase explicitly so an initial INTAKE value is not retained
                # merely because it is a valid legacy solver phase.
                run.current_phase = RunStatus.REPORTING.value
                transition(run, RunStatus.REPORTING)
                transition(run, RunStatus.COMPLETED_SOLVED)
            elif result.status == RunStatus.COMPLETED_UNSOLVED.value:
                run.current_phase = RunStatus.REPORTING.value
                transition(run, RunStatus.REPORTING)
                transition(run, RunStatus.COMPLETED_UNSOLVED)
            elif result.status == RunStatus.TIMEOUT.value:
                run.current_phase = RunStatus.REPORTING.value
                transition(run, RunStatus.REPORTING)
                transition(run, RunStatus.TIMEOUT)
            else:
                if RunStatus(run.status) == RunStatus.RUNNING:
                    transition(run, RunStatus.REPORTING)
                transition(run, RunStatus.FAILED_ENGINE)
                run.last_error_code = result.reason or "MUTEKI_RUNTIME_ERROR"
            evidence_refs = list(
                (
                    await session.scalars(
                        select(EvidenceLedger.id)
                        .where(EvidenceLedger.run_id == run.id)
                        .order_by(EvidenceLedger.created_at)
                    )
                ).all()
            )
            usage = await load_muteki_usage(session, run_id)
            run.report_json = {
                "generated": True,
                "engine": "muteki",
                "status": result.status,
                "reason": result.reason,
                "flag": result.flag if result.flag_found else None,
                "flag_verified": bool(result.flag_found),
                # Values the pipeline recovered that do not fit the challenge's
                # declared banner.  They are NOT solves and are NOT submitted --
                # they are materialised as reviewable candidates so an operator
                # decides, because the usual cause is a value taken from
                # somewhere other than the target.
                "suspected_flags": list(getattr(result, "suspected_flags", ()) or ()),
                "evidence_refs": [str(item) for item in evidence_refs],
                "evidence_count": len(evidence_refs),
                "graph_path": result.graph_path,
                "completed_stages": ["prepare", "race", "coordinator", "finalize"],
                "tool_call_count": int(run.tool_call_count or 0),
            }
            if usage.calls:
                run.report_json.update(
                    {
                        "cost_usd": round(usage.cost_usd, 10),
                        "total_tokens": usage.tokens,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "llm_calls": usage.calls,
                    }
                )
            await session.commit()
            # The runtime owns the solve; the platform owns the flag ledger.
            # Muteki reported its flag only through ``muteki.flag_found``, so
            # ``flag_candidates`` stayed empty and the benchmark evaluator --
            # which reads only that table -- scored a solved case as
            # INCOMPLETE.  Record it here, where the ORM Challenge (and its
            # declared banner) is available, and re-verify the value instead of
            # trusting the runtime's own bit.
            if result.flag_found and result.flag:
                await self._record_runtime_flag_isolated(run_id, str(result.flag))
            for suspected in getattr(result, "suspected_flags", ()) or ():
                # A suspect is informative only, so a failure here is genuinely
                # tolerable -- unlike a real flag, which must reach the ledger.
                await self._record_runtime_flag_isolated(
                    run_id, str(suspected), required=False
                )
            if result.status not in {
                RunStatus.COMPLETED_SOLVED.value,
                RunStatus.COMPLETED_UNSOLVED.value,
            }:
                # The Coordinator owns the terminal handoff.  When the runtime
                # returns without it, the outer Run is about to become
                # terminal while Finalize never landed: keep that anomaly
                # durable instead of closing the Run silently.
                if await self._muteki_finalize_handoff_state(run_id) == "pending":
                    await self._record_muteki_finalize_grace_exceeded(
                        run_id,
                        reason="RUNTIME_RETURNED_WITHOUT_HANDOFF",
                        detail={"runtime_status": str(result.status)},
                    )
            if result.status in {
                RunStatus.COMPLETED_SOLVED.value,
                RunStatus.COMPLETED_UNSOLVED.value,
            }:
                await report_service.generate_muteki(session, run, result)
        except (Exception, SystemExit) as error:
            # ``SystemExit`` is included on purpose.  It is a ``BaseException``,
            # so it slips past every ``except Exception`` between a failed host
            # operation and here -- and one that escapes a background task
            # terminates the whole event loop, taking the API with it.  A Run may
            # fail for any reason; the service may not.  Converting it here means
            # such a failure lands on the Run as FAILED_ENGINE instead.
            await session.rollback()
            # ``rollback`` expires every attribute on ``run``.  Reading
            # ``run.id`` afterwards re-loads it lazily, and that load has no
            # greenlet once the session has already been unwound -- the
            # ``MissingGreenlet`` that escaped this handler and killed the
            # lifecycle coroutine.  ``run_id`` was captured before the try.
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise
            if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED}:
                if RunStatus(run.status) != RunStatus.FAILED_ENGINE:
                    # Muteki failures can occur after the supervisor has
                    # entered RUNNING but before the canonical runtime has
                    # established its own solver phase.  REPORTING is the
                    # lifecycle bridge that legally closes that run; a direct
                    # RUNNING -> FAILED_ENGINE transition is rejected by the
                    # shared state machine and would leave a zombie RUNNING
                    # row with no lease or active task.
                    if RunStatus(run.status) == RunStatus.RUNNING:
                        transition(run, RunStatus.REPORTING)
                    transition(run, RunStatus.FAILED_ENGINE)
                run.last_error_code = "MUTEKI_RUNTIME_ERROR"
                run.last_error_message = str(error)[:4000]
                await session.commit()
            result = type("MutekiFailure", (), {"status": RunStatus.FAILED_ENGINE.value, "reason": "MUTEKI_RUNTIME_ERROR", "flag_found": False})()
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if attempt is not None or lease is not None:
                # ToolGateway and the error handler both commit.  Re-load the
                # lifecycle rows before final accounting so an expired ORM
                # instance cannot trigger a synchronous lazy load after a
                # failed worker (MissingGreenlet on asyncmy).
                await session.rollback()
                current_run = await session.get(SolveRun, run_id, populate_existing=True)
                current_attempt = await session.get(RunAttempt, attempt_id, populate_existing=True) if attempt_id else None
                current_lease = await session.get(RunExecutionLease, lease_id, populate_existing=True) if lease_id else None
                if current_run is not None:
                    run = current_run
                await run_attempt_service.finish(session, run, current_attempt, current_lease)
                if current_run is not None:
                    await session.refresh(current_run)
                    await _reconcile_muteki_run_counters(session, current_run)
                    await session.commit()
            # Last point that still owns this Run's Sandbox name.  A Run about
            # to return to the supervisor has no further owner, so a container
            # left behind now is a leak until the next service restart.
            await _reap_muteki_container(run_id)
        await event_service.append(session, run_id, "run.completed" if result.status.startswith("COMPLETED") else "run.failed", {"engine": "muteki", "status": run.status, "reason": result.reason, "flag_verified": result.flag_found})
        return await self._outcome(session, run)

    async def _heartbeat_muteki_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        lease_id: str | None,
        interval_seconds: float = MUTEKI_ATTEMPT_HEARTBEAT_SECONDS,
    ) -> None:
        """Keep the outer Run lease alive during a canonical Muteki session.

        Muteki deliberately keeps one Sandbox and SharedGraph alive across
        Race/Coordinator turns.  ``RunAttemptService.begin`` creates the
        application lease before that session starts, so without this small
        bridge a healthy native Worker can look like a crashed process to the
        supervisor after the lease TTL.  The heartbeat has no solver or tool
        semantics: it only renews existing lifecycle rows and updates the
        ordinary ``updated_at`` view timestamp for observers.
        """

        if not attempt_id or not lease_id:
            return
        interval = max(0.1, float(interval_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                async with SessionLocal() as heartbeat_session:
                    current_run = await heartbeat_session.get(SolveRun, run_id)
                    if current_run is None or str(current_run.status) in USER_VISIBLE_TERMINAL:
                        return
                    attempt = await heartbeat_session.get(RunAttempt, attempt_id)
                    lease = await heartbeat_session.get(RunExecutionLease, lease_id)
                    if attempt is None or lease is None or attempt.status != "RUNNING":
                        return
                    await run_attempt_service.heartbeat(
                        heartbeat_session,
                        attempt,
                        lease,
                    )
                    # ``heartbeat`` intentionally only touches attempt/lease
                    # rows.  Project the same liveness to the Run list/detail
                    # view without changing any lifecycle status.
                    current_run.updated_at = datetime.now(UTC)
                    await heartbeat_session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Liveness is observability/recovery support.  A transient DB
                # failure must not cancel the canonical Muteki Coordinator;
                # the next interval retries with a fresh session.
                logger.debug(
                    "Muteki attempt heartbeat failed for run %s",
                    run_id,
                    exc_info=True,
                )

    async def run_background(self, run_id: str, user_message: str | None = None) -> RunOutcome:
        lock = self.run_supervisor_lock.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return RunOutcome(run_id, "RUNNING", "", "RUN_ALREADY_RUNNING")
        await lock.acquire()
        task = self._register_current_run_task(run_id)
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            try:
                return await self.continue_until_terminal(session, run_id, user_message)
            except (Exception, SystemExit) as error:
                # This coroutine *is* the Run's lifecycle.  Anything escaping
                # here -- typically a session that died under the runtime --
                # used to end the task and leave the Run row on RUNNING with
                # no owner: ``_active_run_tasks`` is unregistered in the
                # ``finally`` below, so even the cancel endpoint can no longer
                # reach it.  Measured 2026-10-19: a ``MissingGreenlet`` from a
                # ``run.id`` read after ``rollback`` did exactly that and the
                # Run sat at RUNNING until an operator gave up on it.
                logger.exception("run_background crashed run_id=%s", run_id)
                closed = await self._close_crashed_run(run_id, error)
                return RunOutcome(run_id, closed or "FAILED_ENGINE", "", "SUPERVISOR_CRASHED")
            finally:
                self._unregister_run_task(run_id, task)
                self._release_run_lock(run_id, lock)

    async def _close_crashed_run(self, run_id: str, error: BaseException) -> str | None:
        """Force a Run terminal after its lifecycle coroutine died.

        The Run row is the durable projection of the lifecycle, so a coroutine
        that ends without writing it leaves a zombie that nothing owns.  A
        fresh session is used on purpose: the session that raised is the thing
        that broke.

        Returns the status the Run was left in, or ``None`` when the row is
        gone or could not be read at all.  A Run that is already settled is
        reported as-is rather than overwritten: the crash happened *after* the
        useful work, and the caller wants to see that work, not the failure.
        """

        from app.core.database import SessionLocal
        from app.orchestration.state_machine import RunStatus, transition

        try:
            async with SessionLocal() as session:
                run = await session.get(SolveRun, run_id, populate_existing=True)
                if run is None:
                    return None
                status = RunStatus(str(run.status))
                if status in {
                    RunStatus.COMPLETED_SOLVED,
                    RunStatus.COMPLETED_UNSOLVED,
                    RunStatus.TIMEOUT,
                    RunStatus.CANCELLED,
                    RunStatus.FAILED_ENGINE,
                    # WAITING_USER is the one non-terminal status that is
                    # *not* a zombie: a human owns it and can answer.  Marking
                    # it failed here would burn the operator's only way back
                    # into the Run, so a crash while waiting is logged and
                    # left for that person to retry.
                    RunStatus.WAITING_USER,
                }:
                    return str(run.status)
                if status == RunStatus.RUNNING:
                    # The shared state machine has no RUNNING -> FAILED_ENGINE
                    # edge; REPORTING is the lifecycle bridge the error handler
                    # in ``_run_muteki`` uses for the same reason.
                    transition(run, RunStatus.REPORTING)
                transition(run, RunStatus.FAILED_ENGINE)
                run.last_error_code = "SUPERVISOR_CRASHED"
                run.last_error_message = f"{type(error).__name__}: {error}"[:4000]
                await session.commit()
                return RunStatus.FAILED_ENGINE.value
        except Exception:  # noqa: BLE001 - last-resort cleanup must not raise
            logger.exception("unable to close crashed run_id=%s", run_id)
        return None

    async def run_after_user_input_background(self, run_id: str) -> RunOutcome:
        lock = self.run_supervisor_lock.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return RunOutcome(run_id, "RUNNING", "", "RUN_ALREADY_RUNNING")
        await lock.acquire()
        task = self._register_current_run_task(run_id)
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            try:
                outcome = await self.continue_after_user_input(session, run_id)
                if outcome.status in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                    return outcome
                baseline = await self._progress_snapshot(session, run_id)
                await asyncio.sleep(30)
                await session.rollback()
                run = await session.get(SolveRun, run_id)
                if run is None or str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                    return outcome
                current = await self._progress_snapshot(session, run_id)
                if current == baseline:
                    return await self._mark_user_input_no_progress(session, run, current)
                return RunOutcome.from_run(run)
            except (Exception, SystemExit) as error:
                # Same boundary as ``run_background``: this task owns the Run
                # for as long as it lives, and an escape here leaves a row that
                # no endpoint can reach.  The difference is what counts as
                # settled -- a Run that is still WAITING_USER is kept there so
                # the operator can answer again.
                logger.exception("run_after_user_input_background crashed run_id=%s", run_id)
                closed = await self._close_crashed_run(run_id, error)
                return RunOutcome(run_id, closed or "WAITING_USER", "", "SUPERVISOR_CRASHED")
            finally:
                self._unregister_run_task(run_id, task)
                self._release_run_lock(run_id, lock)


run_supervisor = RunSupervisor()


async def continue_until_terminal(session, run_id: str, user_message: str | None = None) -> RunOutcome:
    return await run_supervisor.continue_until_terminal(session, run_id, user_message)


async def continue_after_user_input(session, run_id: str) -> RunOutcome:
    return await run_supervisor.continue_after_user_input(session, run_id)
