"""Durable failure fingerprinting and bounded retry policy."""

import hashlib
import json

from sqlalchemy import select

from app.models.multi_agent import ApprovedAction
from app.models.solver_state import SolverState


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
    counts[fingerprint] = entry
    checkpoint["tool_failure_counts"] = counts
    run.recovery_checkpoint_json = checkpoint
    state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
    if state is not None:
        ledger = dict(state.capability_ledger_json or {})
        ledger_counts = dict(ledger.get("tool_failure_counts") or {})
        ledger_counts[fingerprint] = entry
        state.capability_ledger_json = {**ledger, "tool_failure_counts": ledger_counts}
    await session.commit()
    return entry
