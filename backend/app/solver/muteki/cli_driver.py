"""Standalone process entry point for canonical Muteki workers.

The driver is deliberately a thin boundary: the shared SQLite graph remains
the only coordination state, while this process claims one intent, delegates
execution to one engine, and records only a bounded, unverified result summary.
It also exposes a skill mode so container workers do not need application
imports to read or write the blackboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control import ControlClient
from .graph import MutekiGraph
from .identity import EngineType, IdentityModel
from .worker.engine import get_engine
from .worker.pool import WorkerPool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CLIDriver:
    """Host-side launcher for one standalone worker process."""

    def __init__(self, *, python: str | None = None, timeout_seconds: int = 900) -> None:
        self.python = python or sys.executable
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.backend_root = Path(__file__).resolve().parents[3]

    async def run_worker(
        self,
        *,
        intent_id: str,
        engine: str,
        workspace: str,
        blackboard: str,
        worker_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> ProcessResult:
        command = [
            self.python,
            "-m",
            "app.solver.muteki.cli_driver",
            "--mode",
            "worker",
            "--engine",
            engine,
            "--intent-id",
            intent_id,
            "--workspace",
            workspace,
            "--blackboard",
            blackboard,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = _prepend_path(str(self.backend_root), env.get("PYTHONPATH", ""))
        if worker_id:
            env["MUTEKI_WORKER_ID"] = worker_id
        return await self._run(command, env=env, timeout_seconds=timeout_seconds)

    async def run_skill(self, *, blackboard: str, command: str, skill_args: list[str] | None = None) -> ProcessResult:
        skill_path = Path(__file__).with_name("skill") / "blackboard.py"
        argv = [self.python, str(skill_path), command, *(skill_args or [])]
        env = os.environ.copy()
        env["MUTEKI_BLACKBOARD_DB"] = blackboard
        return await self._run(argv, env=env, timeout_seconds=60)

    async def _run(self, command: list[str], *, env: dict[str, str], timeout_seconds: int | None) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, int(timeout_seconds or self.timeout_seconds)),
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ProcessResult(-1, stderr="PROCESS_TIMEOUT", timed_out=True)
        return ProcessResult(
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace")[-12000:],
            stderr.decode("utf-8", errors="replace")[-4000:],
        )


async def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    blackboard = _required_path(args.blackboard or os.environ.get("MUTEKI_BLACKBOARD_DB"), "blackboard")
    workspace = _required_path(args.workspace or os.environ.get("MUTEKI_WORKSPACE"), "workspace")
    worker_id = args.worker_id or os.environ.get("MUTEKI_WORKER_ID") or f"cli-{os.getpid()}"
    challenge_id = os.environ.get("MUTEKI_CHALLENGE_ID", "cli-run")
    graph = MutekiGraph(blackboard, challenge_id=challenge_id)
    try:
        intent = next((item for item in graph.intents() if item.intent_id == args.intent_id), None)
        if intent is None:
            return {"status": "FAILED", "reason": "INTENT_NOT_FOUND", "intent_id": args.intent_id}
        if not graph.claim_intent(worker=worker_id, intent_id=intent.intent_id):
            return {"status": "SKIPPED", "reason": "INTENT_CLAIM_LOST", "intent_id": intent.intent_id}
        control = ControlClient(worker_id)
        await control.send_heartbeat()
        initial_command = await control.check_control()
        if initial_command and initial_command.type in {"pause", "cancel"}:
            if initial_command.type == "pause":
                graph.release_intent(worker=worker_id, intent_id=intent.intent_id)
            else:
                graph.conclude_intent(actor=worker_id, intent_id=intent.intent_id, result="CANCELLED")
            await control.report_status({"status": initial_command.type.upper()})
            return {"status": initial_command.type.upper(), "intent_id": intent.intent_id}
        engine = get_engine(args.engine, cli=True)
        execution = asyncio.create_task(WorkerPool({args.engine: engine}).execute(intent, workspace, preferred=args.engine))
        while not execution.done():
            await asyncio.sleep(0.25)
            command = await control.check_control()
            if command and command.type in {"pause", "cancel"}:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                if command.type == "pause":
                    graph.release_intent(worker=worker_id, intent_id=intent.intent_id)
                    status = "PAUSED"
                else:
                    graph.conclude_intent(actor=worker_id, intent_id=intent.intent_id, result="CANCELLED")
                    status = "CANCELLED"
                await control.report_status({"status": status})
                return {"status": status, "intent_id": intent.intent_id}
        result = await execution
        output_summary = result.output[-2000:] if result.output else ""
        if result.success:
            graph.add_fact(
                actor=worker_id,
                content=json.dumps({"type": "WORKER_RESULT", "engine": result.engine_type, "success": True, "output_summary": output_summary}, ensure_ascii=False),
                verified=False,
                dedupe_key=f"worker-result:{intent.intent_id}",
            )
            status = "COMPLETED"
        else:
            reason = str(result.metadata.get("reason") or "ENGINE_FAILED")
            graph.add_dead_end(actor=worker_id, description=f"{intent.description}: {reason}")
            status = "FAILED"
        graph.conclude_intent(actor=worker_id, intent_id=intent.intent_id, result=status)
        await control.report_status({"status": status, "engine": result.engine_type})
        for resource_id in _resource_ids(intent.payload):
            graph.release_resource(worker=worker_id, resource_id=resource_id)
        return {"status": status, "intent_id": intent.intent_id, "engine": result.engine_type, "metadata": result.metadata}
    finally:
        graph.close()


async def run_skill(args: argparse.Namespace) -> dict[str, Any]:
    blackboard = _required_path(args.blackboard or os.environ.get("MUTEKI_BLACKBOARD_DB"), "blackboard")
    driver = CLIDriver(timeout_seconds=60)
    result = await driver.run_skill(blackboard=blackboard, command=args.skill_cmd, skill_args=args.skill_args or [])
    return {"status": "COMPLETED" if result.returncode == 0 else "FAILED", "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def check_health(engine_name: str) -> bool:
    if engine_name == "all":
        return any(IdentityModel.detect_engine(name) is not EngineType.UNKNOWN for name in ("codex", "claude", "cursor"))
    return IdentityModel.detect_engine(engine_name) is not EngineType.UNKNOWN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Muteki standalone worker driver")
    parser.add_argument("--mode", choices=("worker", "skill", "health"), required=True)
    parser.add_argument("--engine", choices=("codex", "claude", "cursor", "all"), default="codex")
    parser.add_argument("--intent-id")
    parser.add_argument("--worker-id")
    parser.add_argument("--workspace")
    parser.add_argument("--blackboard")
    parser.add_argument("--skill-cmd")
    # REMAINDER intentionally preserves skill flags such as --verified and
    # --evidence-ref for the stdlib skill parser.
    parser.add_argument("--skill-args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "health":
        return 0 if check_health(args.engine) else 1
    if args.mode == "worker":
        if not args.intent_id:
            raise SystemExit("--intent-id is required in worker mode")
        result = asyncio.run(run_worker(args))
    else:
        if not args.skill_cmd:
            raise SystemExit("--skill-cmd is required in skill mode")
        result = asyncio.run(run_skill(args))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in {"COMPLETED", "SKIPPED"} else 1


def _required_path(value: str | None, name: str) -> str:
    if not value:
        raise SystemExit(f"{name} path is required")
    return str(Path(value).expanduser().resolve())


def _prepend_path(value: str, existing: str) -> str:
    return value if not existing else value + os.pathsep + existing


def _resource_ids(payload: dict[str, Any] | None) -> tuple[str, ...]:
    values = (payload or {}).get("resource_ids", ())
    if isinstance(values, str):
        return (values,)
    if isinstance(values, (list, tuple, set)):
        return tuple(str(item) for item in values if str(item))
    return ()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CLIDriver", "ProcessResult", "build_parser", "check_health", "main", "run_skill", "run_worker"]
