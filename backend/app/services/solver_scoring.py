"""Challenge-level prediction and per-run scoring for the Solver.

Prediction is a challenge-level model analysis reused by every Run of the
same challenge.  Scoring snapshots the prediction baseline so later refreshes
or weight changes never rewrite historical results.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.models.run import RunAttempt, SolveRun
from app.models.scoring import ChallengePrediction, RunScore
from app.services.events import event_service
from app.solver.muteki.adapter.cost_bridge import usage_from_events

FORMULA_VERSION = "1.0"
SOLVED_WEIGHT = 50.0
TIME_WEIGHT = 30.0
TOKEN_WEIGHT = 20.0
EFFICIENCY_CAP = 2.0

_SAFE_ENVIRONMENT_KEYS = ("adapter", "dbms", "framework", "language", "service", "technology")
_DIFFICULTIES = {"easy", "medium", "hard"}
_locks: dict[str, asyncio.Lock] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def _lock_for(challenge_id: str) -> asyncio.Lock:
    return _locks.setdefault(str(challenge_id), asyncio.Lock())


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def challenge_fingerprint(challenge: Any, attachments: list[Any]) -> str:
    """Stable fingerprint of challenge content that affects prediction."""

    manifest = sorted(
        (
            {
                "name": str(getattr(item, "original_name", "") or ""),
                "kind": str(getattr(item, "kind", "") or ""),
                "size": int(getattr(item, "size", 0) or 0),
                "sha256": str(getattr(item, "sha256", "") or ""),
                "primary": bool(getattr(item, "is_primary", False)),
            }
            for item in attachments
        ),
        key=lambda item: item["sha256"],
    )
    payload = {
        "name": str(getattr(challenge, "name", "") or ""),
        "description": str(getattr(challenge, "description", "") or ""),
        "challenge_type": str(getattr(challenge, "challenge_type", "") or ""),
        "target_url": str(getattr(challenge, "target_url", "") or "") or None,
        "metadata": getattr(challenge, "metadata_json", None) or {},
    }
    raw = json.dumps({"challenge": payload, "attachments": manifest}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_prediction_inputs(challenge: Any, attachments: list[Any]) -> dict[str, Any]:
    metadata = getattr(challenge, "metadata_json", None) or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    safe_metadata = {
        key: metadata[key]
        for key in _SAFE_ENVIRONMENT_KEYS
        if key in metadata and isinstance(metadata[key], (str, int, float, bool))
    }
    manifest = [
        {
            "name": str(item.original_name or "")[:120],
            "kind": str(item.kind or "")[:20],
            "size": int(item.size or 0),
            "primary": bool(item.is_primary),
        }
        for item in attachments
    ][:32]
    return {
        "name": str(getattr(challenge, "name", "") or "")[:200],
        "description": str(getattr(challenge, "description", "") or "")[:4000],
        "challenge_type": str(getattr(challenge, "challenge_type", "") or "")[:40],
        "target_url": str(getattr(challenge, "target_url", "") or "")[:2048] or None,
        "metadata": safe_metadata,
        "attachments": manifest,
    }


def _prediction_prompt(inputs: Mapping[str, Any]) -> str:
    payload = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))
    return (
        "你是 Web 安全靶场出题助手。根据题目信息预测一次自动化解题任务的耗时和消耗，"
        "不要泄露或猜测 Flag，不要输出源码细节。\n"
        "只输出 JSON，字段：\n"
        '{"predicted_solve_seconds": 整数 30-86400, '
        '"predicted_tokens": 整数 1000-5000000, '
        '"predicted_tool_calls": 整数 1-1000, '
        '"difficulty": "easy|medium|hard", '
        '"confidence": 0-1 浮点数, '
        '"rationale_zh": 不超过 300 字的中文理由}\n'
        f"题目信息：{payload}"
    )


def _is_prediction_model_config(config: Any) -> bool:
    """Return whether a model config can serve Coordinator Reason requests."""

    roles = (getattr(config, "capabilities_json", None) or {}).get("roles", ["worker"])
    return bool(
        config
        and getattr(config, "enabled", False)
        and getattr(config, "provider_type", None) == "openai_compatible"
        and "coordinator_reason" in roles
        and getattr(config, "encrypted_api_key", None)
        and getattr(config, "base_url", None)
    )


def _select_prediction_model_config(
    configs: Iterable[Any],
    *,
    reason_model_config_id: str | None = None,
) -> Any:
    """Select the explicit Reason config, or the first valid Reason fallback."""

    candidates = list(configs)
    if reason_model_config_id:
        selected = next(
            (
                config
                for config in candidates
                if str(getattr(config, "id", "")) == str(reason_model_config_id)
            ),
            None,
        )
        if not _is_prediction_model_config(selected):
            raise ValueError("PREDICTION_MODEL_UNAVAILABLE")
        return selected

    for config in candidates:
        if _is_prediction_model_config(config):
            return config
    raise ValueError("PREDICTION_MODEL_UNAVAILABLE")


async def _resolve_prediction_model_config(
    session: AsyncSession,
    *,
    challenge_id: str | None,
    reason_model_config_id: str | None,
) -> Any:
    """Resolve the Reason model for a challenge prediction.

    Run creation passes the current Run's Reason selection explicitly.
    Challenge-level refreshes inherit the newest Run's selection.  Only legacy
    challenges without a usable Run selection use the global fallback.
    """

    from app.models.model_config import ModelConfig
    from app.solver.muteki.runtime.configuration import runtime_selection_from_hints

    selected_id = str(reason_model_config_id).strip() if reason_model_config_id else None
    if selected_id is None and challenge_id:
        latest_run = await session.scalar(
            select(SolveRun)
            .where(SolveRun.challenge_id == challenge_id)
            .order_by(SolveRun.created_at.desc())
            .limit(1)
        )
        if latest_run is not None:
            selected_id, _ = runtime_selection_from_hints(latest_run.hints_json)

    if selected_id:
        config = await session.get(ModelConfig, selected_id)
        return _select_prediction_model_config(
            (config,) if config is not None else (),
            reason_model_config_id=selected_id,
        )

    configs = list(
        (
            await session.scalars(
                select(ModelConfig)
                .where(ModelConfig.enabled, ModelConfig.provider_type == "openai_compatible")
                .order_by(ModelConfig.created_at.asc())
            )
        ).all()
    )
    return _select_prediction_model_config(configs)


def parse_prediction_document(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one model prediction response."""

    if isinstance(raw, str):
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("PREDICTION_INVALID_JSON") from error
    else:
        document = raw
    if not isinstance(document, Mapping):
        raise ValueError("PREDICTION_NOT_OBJECT")

    def _int(name: str, minimum: int, maximum: int) -> int:
        try:
            value = int(document.get(name) or 0)
        except (TypeError, ValueError) as error:
            raise ValueError(f"PREDICTION_INVALID_{name.upper()}") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"PREDICTION_OUT_OF_RANGE_{name.upper()}")
        return value

    difficulty = str(document.get("difficulty") or "medium").strip().lower()
    if difficulty not in _DIFFICULTIES:
        difficulty = "medium"
    try:
        confidence = float(document.get("confidence") or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    rationale = str(document.get("rationale_zh") or "").strip()[:600]
    return {
        "predicted_solve_seconds": _int("predicted_solve_seconds", 30, 86400),
        "predicted_tokens": _int("predicted_tokens", 1000, 5_000_000),
        "predicted_tool_calls": _int("predicted_tool_calls", 1, 1000),
        "difficulty": difficulty,
        "confidence": round(confidence, 4),
        "rationale_zh": rationale,
    }


def compute_score_components(
    *,
    predicted_seconds: int,
    predicted_tokens: int,
    actual_seconds: float | None,
    actual_tokens: int | None,
    solved: bool,
) -> dict[str, Any]:
    """Pure scoring formula shared by terminal finalization and tests."""

    if predicted_seconds <= 0 or predicted_tokens <= 0:
        raise ValueError("prediction values must be positive")
    time_ratio: float | None = None
    token_ratio: float | None = None
    time_eff = 0.0
    token_eff = 0.0
    if actual_seconds is not None and actual_seconds > 0:
        time_ratio = actual_seconds / predicted_seconds
        time_eff = predicted_seconds / actual_seconds
    if actual_tokens is not None and actual_tokens > 0:
        token_ratio = actual_tokens / predicted_tokens
        token_eff = predicted_tokens / actual_tokens
    elif actual_tokens is not None and actual_tokens == 0:
        token_ratio = 0.0
        token_eff = EFFICIENCY_CAP
    time_points = TIME_WEIGHT * min(max(time_eff, 0.0), EFFICIENCY_CAP)
    token_points = TOKEN_WEIGHT * min(max(token_eff, 0.0), EFFICIENCY_CAP)
    solved_points = SOLVED_WEIGHT if solved else 0.0
    return {
        "time_ratio": round(time_ratio, 6) if time_ratio is not None else None,
        "token_ratio": round(token_ratio, 6) if token_ratio is not None else None,
        "time_points": round(time_points, 2),
        "token_points": round(token_points, 2),
        "solved_points": solved_points,
        "total_score": round(solved_points + time_points + token_points, 2),
    }


def prediction_read(item: ChallengePrediction) -> dict[str, Any]:
    return {
        "id": item.id,
        "challenge_id": item.challenge_id,
        "prediction_version": item.prediction_version,
        "status": item.status,
        "fingerprint": item.fingerprint,
        "predicted_solve_seconds": item.predicted_solve_seconds,
        "predicted_tokens": item.predicted_tokens,
        "predicted_tool_calls": item.predicted_tool_calls,
        "difficulty": item.difficulty,
        "confidence": item.confidence,
        "rationale_zh": item.rationale_zh,
        "model": item.model,
        "usage": item.usage_json or {},
        "error_code": item.error_code,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def run_score_read(item: RunScore) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "challenge_id": item.challenge_id,
        "prediction_snapshot": item.prediction_snapshot_json or {},
        "actual_seconds": item.actual_seconds,
        "actual_tokens": item.actual_tokens,
        "solved": item.solved,
        "time_ratio": item.time_ratio,
        "token_ratio": item.token_ratio,
        "time_points": item.time_points,
        "token_points": item.token_points,
        "solved_points": item.solved_points,
        "total_score": item.total_score,
        "formula_version": item.formula_version,
        "score_status": item.score_status,
        "error_code": item.error_code,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


async def get_prediction(session: AsyncSession, challenge_id: str) -> dict[str, Any] | None:
    item = await session.scalar(
        select(ChallengePrediction).where(ChallengePrediction.challenge_id == challenge_id)
    )
    return prediction_read(item) if item else None


async def _save_prediction(
    session: AsyncSession,
    *,
    challenge_id: str,
    status: str,
    fingerprint: str,
    version: int,
    data: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
    model: str | None = None,
    error_code: str | None = None,
) -> ChallengePrediction:
    item = await session.scalar(
        select(ChallengePrediction).where(ChallengePrediction.challenge_id == challenge_id)
    )
    if item is None:
        item = ChallengePrediction(challenge_id=challenge_id)
        session.add(item)
    item.status = status
    item.fingerprint = fingerprint
    item.prediction_version = version
    item.error_code = error_code
    if data is not None:
        item.predicted_solve_seconds = int(data.get("predicted_solve_seconds") or 0) or None
        item.predicted_tokens = int(data.get("predicted_tokens") or 0) or None
        item.predicted_tool_calls = int(data.get("predicted_tool_calls") or 0) or None
        item.difficulty = str(data.get("difficulty") or "")[:30] or None
        item.confidence = float(data.get("confidence") or 0)
        item.rationale_zh = str(data.get("rationale_zh") or "")[:600] or None
    if usage is not None:
        item.usage_json = dict(usage)
    if model is not None:
        item.model = str(model)[:120]
    await session.commit()
    await session.refresh(item)
    return item


async def _call_prediction_llm(
    inputs: Mapping[str, Any],
    *,
    challenge_id: str | None = None,
    reason_model_config_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, int], str]:
    from app.services.crypto import decrypt_api_key

    async with SessionLocal() as session:
        config = await _resolve_prediction_model_config(
            session,
            challenge_id=challenge_id,
            reason_model_config_id=reason_model_config_id,
        )
        api_key = decrypt_api_key(config.encrypted_api_key)
        model = str(config.model_name or config.name)[:120]
        base_url = str(config.base_url).rstrip("/")
    timeout = httpx.Timeout(connect=10, read=180, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _prediction_prompt(inputs)}],
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
    data = parse_prediction_document(message)
    return (
        data,
        {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        model,
    )


async def _run_prediction_background(
    challenge_id: str,
    fingerprint: str,
    version: int,
    inputs: Mapping[str, Any],
    reason_model_config_id: str | None = None,
) -> None:
    try:
        data, usage, model = await asyncio.wait_for(
            _call_prediction_llm(
                inputs,
                challenge_id=challenge_id,
                reason_model_config_id=reason_model_config_id,
            ),
            timeout=180,
        )
        async with SessionLocal() as session:
            await _save_prediction(
                session,
                challenge_id=challenge_id,
                status="COMPLETED",
                fingerprint=fingerprint,
                version=version,
                data=data,
                usage=usage,
                model=model,
            )
    except TimeoutError:
        async with SessionLocal() as session:
            await _save_prediction(
                session,
                challenge_id=challenge_id,
                status="FAILED",
                fingerprint=fingerprint,
                version=version,
                error_code="PREDICTION_TIMEOUT",
            )
    except Exception as error:
        code = str(error).strip()[:120] or type(error).__name__.upper()[:120]
        async with SessionLocal() as session:
            await _save_prediction(
                session,
                challenge_id=challenge_id,
                status="FAILED",
                fingerprint=fingerprint,
                version=version,
                error_code=code,
            )
    finally:
        _tasks.pop(str(challenge_id), None)


async def request_prediction(
    session: AsyncSession,
    *,
    challenge: Any,
    attachments: list[Any],
    force: bool = False,
    reason_model_config_id: str | None = None,
) -> dict[str, Any]:
    """Reuse a matching prediction or start a background refresh."""

    challenge_id = str(getattr(challenge, "id", "") or "")
    fingerprint = challenge_fingerprint(challenge, attachments)
    async with _lock_for(challenge_id):
        existing = await session.scalar(
            select(ChallengePrediction).where(ChallengePrediction.challenge_id == challenge_id)
        )
        if (
            not force
            and existing is not None
            and existing.status in {"COMPLETED", "RUNNING"}
            and existing.fingerprint == fingerprint
        ):
            return prediction_read(existing)
        version = (existing.prediction_version if existing else 0) + 1
        inputs = build_prediction_inputs(challenge, attachments)
        if existing is None:
            existing = ChallengePrediction(
                challenge_id=challenge_id,
                prediction_version=version,
                status="RUNNING",
                fingerprint=fingerprint,
            )
            session.add(existing)
        else:
            existing.prediction_version = version
            existing.status = "RUNNING"
            existing.fingerprint = fingerprint
            existing.predicted_solve_seconds = None
            existing.predicted_tokens = None
            existing.predicted_tool_calls = None
            existing.difficulty = None
            existing.confidence = None
            existing.rationale_zh = None
            existing.model = None
            existing.error_code = None
        await session.commit()
        await session.refresh(existing)
        task = asyncio.create_task(
            _run_prediction_background(
                challenge_id,
                fingerprint,
                version,
                inputs,
                reason_model_config_id=reason_model_config_id,
            ),
            name=f"challenge-prediction-{challenge_id}",
        )
        _tasks[challenge_id] = task
        return prediction_read(existing)


async def get_run_token_total(session: AsyncSession, run_id: str) -> int:
    """Unified token total: Muteki cost events first, legacy attempts fallback."""

    events = await event_service.history(session, run_id)
    muteki_usage = usage_from_events(events)
    if muteki_usage.tokens > 0:
        return muteki_usage.tokens
    attempts = list(
        (await session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id))).all()
    )
    return sum(
        (int(attempt.input_tokens or 0) + int(attempt.output_tokens or 0))
        for attempt in attempts
    )


async def _upsert_run_score(
    session: AsyncSession,
    *,
    run: SolveRun,
    values: Mapping[str, Any],
) -> RunScore:
    item = await session.scalar(select(RunScore).where(RunScore.run_id == run.id))
    if item is None:
        item = RunScore(run_id=run.id, challenge_id=run.challenge_id)
        session.add(item)
    for key, value in values.items():
        setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item


async def finalize_run_score(session: AsyncSession, run: SolveRun) -> dict[str, Any]:
    """Compute and persist the terminal score for one Run (idempotent)."""

    from app.services.run_finalizer import TERMINAL_RUN_STATUSES

    if str(run.status) not in TERMINAL_RUN_STATUSES:
        existing = await load_run_score(session, run.id)
        if existing is not None:
            return existing
        return {
            "score_status": "PENDING",
            "run_id": run.id,
            "challenge_id": run.challenge_id,
        }
    prediction = await session.scalar(
        select(ChallengePrediction).where(ChallengePrediction.challenge_id == run.challenge_id)
    )
    if (
        prediction is None
        or prediction.status != "COMPLETED"
        or not prediction.predicted_solve_seconds
        or not prediction.predicted_tokens
    ):
        item = await _upsert_run_score(
            session,
            run=run,
            values={
                "prediction_snapshot_json": {},
                "actual_seconds": None,
                "actual_tokens": None,
                "solved": str(run.status) == "COMPLETED_SOLVED",
                "time_ratio": None,
                "token_ratio": None,
                "time_points": None,
                "token_points": None,
                "solved_points": None,
                "total_score": None,
                "formula_version": FORMULA_VERSION,
                "score_status": "NO_PREDICTION",
                "error_code": "PREDICTION_NOT_READY",
            },
        )
        await _emit_score_event(session, run, item)
        return run_score_read(item)

    actual_seconds: float | None = None
    if run.started_at is not None and run.finished_at is not None:
        start = run.started_at
        finish = run.finished_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if finish.tzinfo is None:
            finish = finish.replace(tzinfo=UTC)
        actual_seconds = max(0.0, (finish - start).total_seconds())
    actual_tokens = await get_run_token_total(session, run.id)
    solved = str(run.status) == "COMPLETED_SOLVED"
    components = compute_score_components(
        predicted_seconds=int(prediction.predicted_solve_seconds),
        predicted_tokens=int(prediction.predicted_tokens),
        actual_seconds=actual_seconds,
        actual_tokens=actual_tokens,
        solved=solved,
    )
    missing: list[str] = []
    if actual_seconds is None:
        missing.append("TIME")
    if actual_tokens is None:
        missing.append("TOKEN")
    snapshot = {
        "prediction_version": prediction.prediction_version,
        "predicted_solve_seconds": prediction.predicted_solve_seconds,
        "predicted_tokens": prediction.predicted_tokens,
        "predicted_tool_calls": prediction.predicted_tool_calls,
        "difficulty": prediction.difficulty,
        "confidence": prediction.confidence,
        "rationale_zh": prediction.rationale_zh,
        "model": prediction.model,
        "formula_version": FORMULA_VERSION,
    }
    item = await _upsert_run_score(
        session,
        run=run,
        values={
            "prediction_snapshot_json": snapshot,
            "actual_seconds": round(actual_seconds, 3) if actual_seconds is not None else None,
            "actual_tokens": actual_tokens,
            "solved": solved,
            "time_ratio": components["time_ratio"],
            "token_ratio": components["token_ratio"],
            "time_points": components["time_points"],
            "token_points": components["token_points"],
            "solved_points": components["solved_points"],
            "total_score": components["total_score"],
            "formula_version": FORMULA_VERSION,
            "score_status": "COMPLETED" if not missing else "PARTIAL",
            "error_code": "MISSING_" + "_".join(missing) if missing else None,
        },
    )
    await _emit_score_event(session, run, item)
    return run_score_read(item)


async def _emit_score_event(session: AsyncSession, run: SolveRun, item: RunScore) -> None:
    try:
        await event_service.append(
            session,
            run.id,
            "run.score.computed",
            {
                "score_status": item.score_status,
                "total_score": item.total_score,
                "solved": item.solved,
            },
        )
        await session.commit()
    except Exception:
        await session.rollback()


async def load_run_score(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    item = await session.scalar(select(RunScore).where(RunScore.run_id == run_id))
    return run_score_read(item) if item else None


async def get_run_score(session: AsyncSession, run: SolveRun) -> dict[str, Any] | None:
    """Return persisted score, finalizing lazily when the Run is terminal."""

    from app.services.run_finalizer import TERMINAL_RUN_STATUSES

    item = await session.scalar(select(RunScore).where(RunScore.run_id == run.id))
    if item is None and str(run.status) in TERMINAL_RUN_STATUSES:
        return await finalize_run_score(session, run)
    return run_score_read(item) if item else None
