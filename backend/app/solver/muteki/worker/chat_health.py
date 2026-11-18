"""Health probe for Chat-Completions Worker engines.

The CLI engines are probed by launching one real turn inside the run Sandbox
(``official_worker.health``).  Chat-Completions engines were *deliberately*
excluded from that probe -- and then never probed at all, so a relay that
answers 403, 503, or simply drops the ``tools`` schema still received Workers
and returned ``tool_steps: 0`` instead of being taken out of rotation.

This module is the missing half: one bounded model turn, using the same
``LLMClient`` (and therefore the same endpoint/wire resolution and the same
parameter negotiation) that a real Worker turn uses.  It answers two questions
the event stream could not answer before:

* can this endpoint serve a Worker turn at all, and
* does it come back with a tool call, or with prose we cannot act on.

The second answer is reported but does **not** mark the engine unhealthy: a
capable model may legitimately answer a one-line probe in prose.  Only the
first question can fail closed.
"""

from __future__ import annotations

from typing import Any, Mapping

from muteki.core.llm import LLMClient
from muteki.core.wire import CHAT_COMPLETIONS, normalize_wire_api

from app.core.llm_parameters import parameter_constraint

#: Smallest possible tool a Worker round can be asked to call.  Anything larger
#: would cost real tokens on every Run just to prove reachability.
_PROBE_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "probe",
            "description": "Acknowledge the probe.",
            "parameters": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        },
    },
)

_PROBE_MESSAGES: list[dict[str, str]] = [
    {"role": "user", "content": "Call the probe tool with ok=true."},
]

#: Enough for a short tool call, too small to fund a real answer.
_PROBE_MAX_TOKENS = 64


def profile_api_key(profile: Any) -> str:
    """The credential a Worker launch would hand to this engine."""

    driver = getattr(profile, "driver_profile", None)
    reference = ""
    if isinstance(driver, Mapping):
        reference = str(driver.get("api_key_ref") or "")
    name = reference[4:] if reference.startswith("env:") else "OPENAI_API_KEY"
    environment = getattr(profile, "environment", None) or {}
    return str(environment.get(name) or "")


def _reason_for_error(error: BaseException) -> str:
    import httpx

    status = 0
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, httpx.HTTPStatusError):
            status = int(getattr(current.response, "status_code", 0) or 0)
            break
        current = current.__cause__ or current.__context__
    if status in (401, 403):
        return "MODEL_PERMISSION_DENIED"
    if status == 429:
        return "MODEL_RATE_LIMITED"
    if status >= 500:
        return "MODEL_UNAVAILABLE"
    if status:
        return "MODEL_BAD_REQUEST"
    return "WORKER_HEALTHCHECK_FAILED"


def _probe_body() -> dict[str, Any]:
    return {"temperature": 0.0, "max_tokens": _PROBE_MAX_TOKENS}


async def probe_chat_worker(
    profile: Any, *, timeout_seconds: float = 30.0
) -> tuple[bool, str]:
    """``(healthy, reason_code)`` for one Chat-Completions engine profile."""

    driver = getattr(profile, "driver_profile", None)
    if not isinstance(driver, Mapping):
        return False, "WORKER_PROFILE_INCOMPLETE"
    base_url = str(driver.get("base_url") or "").strip()
    model = str(driver.get("model") or "").strip()
    api_key = profile_api_key(profile)
    if not base_url or not model or not api_key:
        # A profile that cannot authenticate would fail every Worker turn for a
        # reason the Coordinator can act on, so it must not look healthy.
        return False, "WORKER_PROFILE_INCOMPLETE"

    wire_api = normalize_wire_api(driver.get("wire_api"), default=CHAT_COMPLETIONS)
    client = LLMClient(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        overall_timeout=timeout_seconds,
        wire_api=wire_api,
    )
    try:
        body = _probe_body()
        try:
            response = await client.chat(
                model=model,
                messages=list(_PROBE_MESSAGES),
                tools=list(_PROBE_TOOLS),
                stream=False,
                **body,
            )
        except Exception as error:
            constraint = parameter_constraint(error)
            if constraint is None:
                return False, _reason_for_error(error)
            field, value = constraint
            if value is None:
                body.pop(field, None)
            else:
                body[field] = value
            try:
                response = await client.chat(
                    model=model,
                    messages=list(_PROBE_MESSAGES),
                    tools=list(_PROBE_TOOLS),
                    stream=False,
                    **body,
                )
            except Exception as retry_error:
                return False, _reason_for_error(retry_error)
        if response.tool_calls:
            return True, "HEALTHY"
        # Reachable, but the reply carried no tool call.  Reported, not failed:
        # the engine can still serve a turn, it just did not demonstrate one.
        return True, "WORKER_TOOLCALL_UNCONFIRMED"
    except Exception:
        return False, "WORKER_HEALTHCHECK_FAILED"
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


__all__ = ["probe_chat_worker", "profile_api_key"]
