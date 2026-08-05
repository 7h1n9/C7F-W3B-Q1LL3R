"""Durable failure fingerprinting and bounded retry policy."""

import hashlib
import json

from sqlalchemy import select

from app.models.multi_agent import ApprovedAction
from app.models.solver_state import SolverState
from app.services.payload_strategy import payload_strategy_manager


def tool_failure_fingerprint(tool_name: str, error_code: str, stage: str,
                             target_expression: str, compiled_arguments_digest: str) -> str:
    payload = {
        "tool_name": tool_name,
        "error_code": error_code,
        "stage": stage,
        "target_expression": target_expression,
        "compiled_arguments_digest": compiled_arguments_digest,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def record_tool_failure(session, run, approved: ApprovedAction, error_code: str) -> dict:
    args = dict(approved.compiled_arguments_json or {})
    stage = str(args.get("stage") or run.current_phase or "")
    target = str(args.get("target_expression") or "")
    fingerprint = tool_failure_fingerprint(
        approved.tool_name, error_code, stage, target,
        str(approved.compiled_arguments_digest or ""),
    )
    checkpoint = dict(run.recovery_checkpoint_json or {})
    counts = dict(checkpoint.get("tool_failure_counts") or {})
    entry = dict(counts.get(fingerprint) or {})
    entry.update({
        "fingerprint": fingerprint,
        "tool_name": approved.tool_name,
        "error_code": error_code,
        "stage": stage,
        "target_expression": target,
        "compiled_arguments_digest": approved.compiled_arguments_digest,
        "count": int(entry.get("count") or 0) + 1,
    })
    strategy_entry = payload_strategy_manager.record(
        checkpoint,
        tool_name=approved.tool_name,
        stage=stage,
        arguments=args,
        error_code=error_code,
        confidence=None,
        result="FAILURE",
    )
    entry["payload_family"] = strategy_entry["payload_family"]
    entry["payload_status"] = strategy_entry["status"]
    counts[fingerprint] = entry
    checkpoint["tool_failure_counts"] = counts
    run.recovery_checkpoint_json = checkpoint
    state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
    if state is not None:
        attack_history = list(state.attack_strategy_history_json or [])
        attack_entry = payload_strategy_manager.attack_strategy_entry(
            attack_history,
            vulnerability_type="SQL_INJECTION" if approved.tool_name == "sql_boolean_compare" else approved.tool_name,
            target=str(args.get("target_expression") or args.get("test_field") or ""),
            tool_name=approved.tool_name,
            payload_family_name=str(strategy_entry["payload_family"]),
            arguments=args,
            result="FAILURE",
            failure_reason=error_code,
        )
        state.attack_strategy_history_json = [*attack_history, attack_entry][-200:]
        ledger = dict(state.capability_ledger_json or {})
        ledger_counts = dict(ledger.get("tool_failure_counts") or {})
        ledger_counts[fingerprint] = entry
        history = [*list(ledger.get("failure_history") or []), entry][-100:]
        state.capability_ledger_json = {
            **ledger,
            "tool_failure_counts": ledger_counts,
            "failure_history": history,
        }
    await session.commit()
    return entry


def blocked_failure_for_action(run, tool_name: str, arguments: dict, arguments_digest: str) -> dict | None:
    """Return the open circuit matching a compiled action, if any.

    The error code is intentionally not part of this lookup: once the exact
    tool/stage/target/argument contract has failed twice, a changed error
    label must not provide a route to a third identical execution.
    """
    stage = str(arguments.get("stage") or run.current_phase or "")
    target = str(arguments.get("target_expression") or "")
    counts = (run.recovery_checkpoint_json or {}).get("tool_failure_counts") or {}
    for entry in counts.values():
        if not isinstance(entry, dict):
            continue
        if (
            int(entry.get("count") or 0) >= 2
            and entry.get("tool_name") == tool_name
            and entry.get("stage") == stage
            and entry.get("target_expression", "") == target
            and str(entry.get("compiled_arguments_digest") or "") == str(arguments_digest or "")
        ):
            return entry
    return None
