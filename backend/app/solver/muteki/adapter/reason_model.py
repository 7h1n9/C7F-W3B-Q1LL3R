"""Coordinator-only Reason model adapter for the canonical Muteki loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from app.core.llm_parameters import parameter_constraint
from app.engines.openai_compatible import OpenAICompatibleEngine

from ..reason import ReasonProvider
from app.engines.content_policy import is_content_policy_error, soften_prompt_for_audit
from .upstream_reason import UpstreamReasonProposals

logger = logging.getLogger(__name__)

REASON_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["explore", "course_correct", "complete"]},
        "goal_met": {"type": "boolean"},
        "complete_why": {"type": "string"},
        "drift": {"type": "string"},
        "intents": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "worker_class": {"type": "string"},
                    "rationale": {"type": "string"},
                    "route_hash": {"type": "string"},
                    "branch_id": {"type": "string"},
                    "lane_key": {"type": "string"},
                    "risk_class": {"type": "string"},
                    "resource_key": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "from": {"type": "array", "items": {"type": "integer"}},
                    "payload": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tool_name": {"type": "string"},
                            "arguments": {"type": "object", "additionalProperties": True},
                            "classification": {"type": "string"},
                            "route_hash": {"type": "string"},
                            "branch_id": {"type": "string"},
                            "lane_key": {"type": "string"},
                            "risk_class": {"type": "string"},
                            "resource_key": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "from": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": [
                            "tool_name", "arguments", "classification", "route_hash",
                            "branch_id", "lane_key", "risk_class", "resource_key",
                            "depends_on", "from",
                        ],
                    },
                },
                "required": [
                    "id", "goal", "worker_class", "rationale", "route_hash",
                    "branch_id", "lane_key", "risk_class", "resource_key",
                    "depends_on", "from", "payload",
                ],
            },
        },
    },
    "required": ["verdict", "goal_met", "complete_why", "drift", "intents"],
}


class _ParameterNegotiatingEngine:
    """Chat boundary that survives a model accepting only ONE parameter value.

    The Reason planner asks for ``temperature=0.3`` (deterministic planning is
    the whole point) and the health probe asks for ``0.0``.  Both are reasonable
    requests, and both are rejected outright by a relay whose model exposes a
    single allowed value::

        400 {"error": {"message": "field temperature invalid, only 1 is allowed
                       for this model", "type": "invalid_request_error"}}

    Read as a payload defect, that kills the Reason phase: the Coordinator falls
    back to deterministic strategy, so the retrieved knowledge prior — the thing
    that tells the swarm which path to hit — never reaches a Worker.  Measured
    on run ``e4320288``: the prior was correct at the retrieval layer
    (``cve-2017-10271-weblogic-ws-at``, score 115.27) while ``wsl-wsat`` got zero
    hits in the worker steps.

    Negotiation is per-field and remembered, so at most one extra request is
    ever spent per model, and a second rejection is reported untouched.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._overrides: dict[str, float] = {}
        self.negotiations: list[str] = []

    def __getattr__(self, name: str) -> Any:
        # ``last_trace``, ``api_key``, ``close`` and friends all belong to the
        # wrapped engine.  Read through ``__dict__`` so a lookup before
        # ``_engine`` exists raises AttributeError instead of recursing.
        engine = self.__dict__.get("_engine")
        if engine is None:
            raise AttributeError(name)
        return getattr(engine, name)

    async def chat(self, **kwargs: Any) -> Any:
        for field, value in self._overrides.items():
            if field in kwargs:
                kwargs[field] = value
        try:
            return await self._engine.chat(**kwargs)
        except Exception as error:  # noqa: BLE001 - decide below whether to retry
            if isinstance(error, asyncio.CancelledError):
                raise
            constraint = parameter_constraint(error)
            if constraint is None:
                raise
            field, allowed = constraint
            if allowed is None or field not in kwargs:
                # The engine always writes the field, so "omit it" is not a
                # retry we can perform here — report the rejection as-is.
                raise
            self._overrides[field] = allowed
            self.negotiations.append(f"{field}={allowed}")
            logger.warning(
                "Reason model %s rejects the %s we send; retrying once with %s",
                kwargs.get("model", ""),
                field,
                allowed,
            )
            kwargs[field] = allowed
            return await self._engine.chat(**kwargs)


class CoordinatorReasonModel:
    """Call one configured OpenAI-compatible model for Coordinator Reason.

    The model only proposes typed graph Intents.  It never receives a Tool
    Gateway handle and never executes a Worker.  On provider failure the
    injected deterministic Muteki strategy remains the bounded fallback.
    """

    def __init__(
        self,
        engine: OpenAICompatibleEngine,
        *,
        model_config_id: str,
        model_name: str,
        fallback: ReasonProvider | None = None,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        run_id: str | None = None,
        challenge_id: str | None = None,
        goal: str | None = None,
        mode: str = "ctf",
        knowledge_prior: str = "",
        challenge_brief: Sequence[str] | None = None,
        health_timeout_seconds: float = 15.0,
        on_content_policy_retry: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.engine = engine
        # The planner speaks to the model through the negotiating wrapper so a
        # relay that pins one parameter value cannot silently disable Reason.
        self.negotiating_engine = _ParameterNegotiatingEngine(engine)
        self.model_config_id = str(model_config_id)
        self.model_name = str(model_name)
        self.fallback = fallback
        self.usage_recorder = usage_recorder
        self.run_id = str(run_id or "")
        self.challenge_id = str(challenge_id or "")
        self.goal = str(goal or "")
        self.mode = str(mode or "ctf")
        self.knowledge_prior = str(knowledge_prior or "")[:8_000]
        # The challenge's own declarations (endpoint, injectable parameter,
        # fixed control values).  Kept separate from ``knowledge_prior`` on
        # purpose: a prior is a retrieved *hypothesis* and is rendered under an
        # "unverified" heading, while this is the case's ground truth.  Mixing
        # them would tell the Reason model to distrust the one input it should
        # treat as given.
        self.challenge_brief = [
            str(item).strip() for item in (challenge_brief or ()) if str(item).strip()
        ][:24]
        self.health_timeout_seconds = max(1.0, float(health_timeout_seconds))
        self.last_error_code = ""
        self.last_health_error_code = ""
        self.last_error_detail = ""
        self.last_health_error_detail = ""
        self.health_attempts = 0
        self.consecutive_failures = 0
        #: How many times this provider has rescued a call by softening the
        #: prompt.  Kept for the Run audit stream and for tests; a healthy Run
        #: stays at zero.
        self.content_policy_retries = 0
        self.on_content_policy_retry = on_content_policy_retry

    @property
    def needs_reactivation(self) -> bool:
        """Whether the last failure is a transient provider condition.

        Contract, credential, permission, and quota failures are not retried
        as health probes.  Only transport/timeouts/rate limits can recover
        without changing the model configuration.
        """

        return self.last_error_code in {
            "MODEL_UNAVAILABLE",
            "MODEL_TIMEOUT",
            "MODEL_RATE_LIMITED",
        }

    async def __call__(self, snapshot: Mapping[str, Any]) -> UpstreamReasonProposals | Any:
        try:
            summary = str(snapshot.get("_reason_summary") or _safe_graph_summary(snapshot))
            fact_index = str(snapshot.get("_fact_pin_context") or "")
            if self.challenge_brief:
                # Declared before the retrieved prior, and labelled as the
                # Challenge's own statement rather than as evidence recovered
                # from the target: the parameter names here are the ones the
                # application actually accepts, which is precisely what a
                # Reason model cannot guess.
                declared = "\n".join(f"- {line}" for line in self.challenge_brief)
                summary = (
                    f"{summary}\n\n"
                    "## Challenge declared surface\n"
                    "Declared by the Challenge record for this Run. Use these "
                    "names, paths and control values when writing an Intent; do "
                    "not invent alternative parameter names.\n\n"
                    f"{declared}"
                )
            if self.knowledge_prior:
                summary = (
                    f"{summary}\n\n"
                    "## Unverified knowledge prior\n"
                    "The following retrieved material is not graph evidence and is not a "
                    "VerifiedFact. Treat it only as a hypothesis source. Independently "
                    "validate every claim against the current Run before relying on it.\n\n"
                    f"{self.knowledge_prior}"
                )
            async def _invoke(prompt_summary: str) -> Any:
                if callable(getattr(self.engine, "chat", None)):
                    # Follow the official Muteki boundary: run_reason owns the
                    # prompt, plain Chat Completion request, and response parser.
                    # This is intentionally separate from Worker action schemas.
                    from muteki.solver.reason import run_reason

                    return await run_reason(
                        llm=self.negotiating_engine,
                        model=self.model_name,
                        graph_summary=prompt_summary,
                        fact_index=fact_index,
                        max_intents=4,
                        run_id=self.run_id or str(snapshot.get("run_id") or "") or None,
                        challenge_id=self.challenge_id or str(snapshot.get("challenge_id") or "") or None,
                        goal=self.goal or None,
                        mode=self.mode,
                    )
                # Compatibility for isolated callers that provide the old
                # minimal fake engine. Production OpenAICompatibleEngine has
                # ``chat`` and always takes the official path above.
                from muteki.solver.reason import build_reason_prompt

                messages = build_reason_prompt(
                    prompt_summary, max_intents=4, goal=self.goal or None, mode=self.mode
                )
                return await self.engine.next_contract(
                    messages,
                    REASON_CONTRACT_SCHEMA,
                    name="muteki_coordinator_reason",
                )

            try:
                contract = await _invoke(summary)
            except Exception as policy_error:  # noqa: BLE001 - narrow check below
                if not is_content_policy_error(policy_error):
                    raise
                # The credential and the endpoint are both fine; the relay's
                # audit objected to the wording.  Retry ONCE with prose softened
                # and payloads untouched.  A no-op rewrite is not worth a second
                # request, so that case is re-raised as the original failure.
                softened = soften_prompt_for_audit(summary)
                if softened == summary:
                    raise
                logger.warning(
                    "Reason model %s rejected the prompt on content policy; "
                    "retrying once with softened wording",
                    self.model_name,
                )
                self.content_policy_retries += 1
                if self.on_content_policy_retry is not None:
                    try:
                        self.on_content_policy_retry(
                            {
                                "model": self.model_name,
                                "run_id": self.run_id,
                                "challenge_id": self.challenge_id,
                                "detail": _safe_error_detail(self.engine, policy_error),
                            }
                        )
                    except Exception:
                        # Observability must never turn a recoverable rejection
                        # into a failed Reason call.
                        pass
                contract = await _invoke(softened)
            # Error state belongs to this provider call, not to the lifetime
            # of the adapter.  A previous outage must not leak into a later
            # successful ``reason.completed`` event.
            proposals = _to_proposals(contract)
            self.last_error_code = ""
            self.last_health_error_code = ""
            self.last_error_detail = ""
            self.last_health_error_detail = ""
            self.consecutive_failures = 0
            if self.usage_recorder is not None:
                try:
                    await self.usage_recorder(dict(self.engine.last_trace or {}))
                except Exception:
                    # Usage is observability only; it must not turn a
                    # successful Reason call into a provider failure.
                    pass
            return proposals
        except Exception as error:
            self.last_error_code = _reason_error_code(error)
            self.last_error_detail = _safe_error_detail(self.engine, error)
            self.consecutive_failures += 1
            if self.fallback is None:
                return UpstreamReasonProposals([], verdict="explore", drift="")
            fallback_result = self.fallback(snapshot)
            if inspect.isawaitable(fallback_result):
                fallback_result = await fallback_result
            return fallback_result

    async def health(self) -> tuple[bool, str]:
        """Probe the configured Reason endpoint without planning or tools.

        This is the Coordinator-side reactivation hook.  It uses the same
        plain Chat Completion boundary as the official Reason path, but a
        tiny prompt and bounded timeout.  A successful probe only restores
        provider availability; it never creates an Intent or changes the
        Blackboard.
        """

        self.health_attempts += 1
        try:
            result = await asyncio.wait_for(
                self.negotiating_engine.chat(
                    model=self.model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a health probe. Reply with OK.",
                        },
                        {"role": "user", "content": "health"},
                    ],
                    temperature=0.0,
                    max_tokens=64,
                    stream=False,
                    run_id=self.run_id or None,
                    challenge_id=self.challenge_id or None,
                    solver_id="reason-health",
                ),
                timeout=self.health_timeout_seconds,
            )
            empty_content = not str(getattr(result, "content", "") or "").strip()
            # Reasoning models may spend the small completion budget on
            # reasoning_content.  A 200 response with no visible content is
            # still a successful endpoint handshake.
            self.last_error_code = ""
            self.last_health_error_code = ""
            self.last_error_detail = ""
            self.last_health_error_detail = ""
            self.consecutive_failures = 0
            if self.usage_recorder is not None:
                try:
                    trace = dict(self.engine.last_trace or {})
                    trace["call_type"] = "reason_health"
                    trace["empty_content"] = empty_content
                    await self.usage_recorder(trace)
                except Exception:
                    pass
            return True, "HEALTHY_EMPTY_CONTENT" if empty_content else "HEALTHY"
        except Exception as error:
            code = _reason_error_code(error)
            self.last_health_error_code = code
            self.last_health_error_detail = _safe_error_detail(self.engine, error)
            return False, code

    async def close(self) -> None:
        await self.engine.close()


def _to_proposals(contract: Mapping[str, Any]) -> UpstreamReasonProposals:
    if not isinstance(contract, Mapping):
        return _reason_result_to_proposals(contract)
    proposals: list[dict[str, Any]] = []
    for raw in contract.get("intents", [])[:4] if isinstance(contract.get("intents"), list) else []:
        if not isinstance(raw, Mapping) or not str(raw.get("goal") or "").strip():
            continue
        payload = dict(raw.get("payload") or {}) if isinstance(raw.get("payload"), Mapping) else {}
        for key in ("route_hash", "branch_id", "lane_key", "risk_class", "resource_key", "depends_on", "from"):
            if key in raw and key not in payload:
                payload[key] = raw[key]
        proposals.append(
            {
                "goal": str(raw["goal"]).strip(),
                "worker_class": str(raw.get("worker_class") or "code"),
                "rationale": str(raw.get("rationale") or ""),
                "payload": payload,
            }
        )
    return UpstreamReasonProposals(
        proposals,
        goal_met=bool(contract.get("goal_met")),
        verdict=str(contract.get("verdict") or "explore"),
        drift=str(contract.get("drift") or ""),
        complete_why=str(contract.get("complete_why") or ""),
    )


def _reason_result_to_proposals(result: Any) -> UpstreamReasonProposals:
    """Project the official ``ReasonResult`` without losing its envelope."""

    proposals: list[dict[str, Any]] = []
    for intent in list(getattr(result, "intents", ()) or ())[:4]:
        goal = str(getattr(intent, "goal", "") or "").strip()
        if not goal:
            continue
        payload = {
            key: value
            for key, value in {
                "route_hash": getattr(intent, "route_hash", ""),
                "branch_id": getattr(intent, "branch_id", ""),
                "lane_key": getattr(intent, "lane_key", ""),
                "risk_class": getattr(intent, "risk_class", ""),
                "resource_key": getattr(intent, "resource_key", ""),
                "depends_on": list(getattr(intent, "depends_on", ()) or ()),
                "from_facts": list(getattr(intent, "from_facts", ()) or ()),
            }.items()
            if value not in (None, "", [], ())
        }
        proposals.append(
            {
                "goal": goal,
                "worker_class": str(getattr(intent, "worker_class", "code") or "code"),
                "rationale": str(getattr(intent, "rationale", "") or ""),
                "payload": payload,
            }
        )
    return UpstreamReasonProposals(
        proposals,
        goal_met=bool(getattr(result, "goal_met", False)),
        verdict=str(getattr(result, "verdict", "explore") or "explore"),
        drift=str(getattr(result, "drift", "") or ""),
        complete_why=str(getattr(result, "complete_why", "") or ""),
    )


def _safe_graph_summary(snapshot: Mapping[str, Any]) -> str:
    """Project graph control state, not raw Worker output, into the prompt."""

    facts = []
    for item in snapshot.get("facts", ()) if isinstance(snapshot.get("facts"), list) else ():
        if not isinstance(item, Mapping):
            continue
        facts.append(
            {
                "id": item.get("fact_id"),
                "content": str(item.get("content") or "")[:800],
                "verified": bool(item.get("verified")),
                "evidence": len(item.get("evidence_refs") or ()) if isinstance(item.get("evidence_refs"), (list, tuple)) else 0,
            }
        )
    intents = []
    for item in snapshot.get("intents", ()) if isinstance(snapshot.get("intents"), list) else ():
        if isinstance(item, Mapping):
            payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
            intents.append(
                {
                    "id": item.get("intent_id"),
                    "goal": str(item.get("description") or "")[:500],
                    "status": item.get("status"),
                    "route_hash": str(item.get("route_hash") or payload.get("route_hash") or "")[:120],
                    "branch_id": str(item.get("branch_id") or payload.get("branch_id") or "")[:120],
                    "lane_key": str(payload.get("lane_key") or "")[:120],
                }
            )
    dead_ends = [
        {"id": item.get("dead_end_id"), "description": str(item.get("description") or "")[:500]}
        for item in snapshot.get("dead_ends", ())
        if isinstance(item, Mapping)
    ] if isinstance(snapshot.get("dead_ends"), list) else []
    return json.dumps(
        {
            "challenge_id": str(snapshot.get("challenge_id") or "")[:120],
            "revision": int(snapshot.get("revision") or 0),
            "facts": facts[-40:],
            "intents": intents[-40:],
            "dead_ends": dead_ends[-30:],
            "verified_flag_count": sum(1 for item in snapshot.get("flags", ()) if isinstance(item, Mapping) and item.get("verified_by_gate")),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )[:12000]


def _reason_error_code(error: Exception) -> str:
    text = str(error).upper()
    for code in (
        "MODEL_CONTENT_POLICY",
        "MODEL_RATE_LIMITED",
        "MODEL_AUTH_FAILED",
        "MODEL_QUOTA_EXCEEDED",
        "MODEL_PERMISSION_DENIED",
        "MODEL_TIMEOUT",
        "MODEL_UNAVAILABLE",
        "MODEL_BAD_REQUEST",
    ):
        if code in text:
            return code
    return type(error).__name__.upper()[:80]


def _safe_error_detail(engine: Any, error: Exception) -> str:
    """Keep a bounded provider message without exposing the configured key."""

    text = str(error).strip()
    api_key = str(getattr(engine, "api_key", "") or "")
    if api_key:
        text = text.replace(f"Bearer {api_key}", "<redacted>")
        text = text.replace(api_key, "<redacted>")
    return text[:200]


__all__ = ["CoordinatorReasonModel", "REASON_CONTRACT_SCHEMA"]
