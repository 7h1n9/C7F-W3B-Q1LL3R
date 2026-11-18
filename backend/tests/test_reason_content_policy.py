"""A content-audit rejection must cost one retry, not the whole Reason phase.

The relay answers the SSRF-inclusive prompt with::

    403 {"error":{"message":"内容审计命中风险规则，请调整输入后重试",
                  "type":"content_policy_violation"}}

Measured 2026-10-22 against ``sub.gavinchen.top`` (direct) and
``127.0.0.1:8787`` (reverse proxy): the *same* prompt that the relay audits is
answered normally through the proxy, i.e. the rejection is about wording, not
about the credential or the endpoint.  A Reason phase that dies on it silently
degrades the Run to deterministic strategy -- the knowledge prior never reaches
a Worker.

Every assertion below is falsifiable: removing the softening retry from
``CoordinatorReasonModel.__call__`` makes these tests fail.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.engines.openai_compatible import ModelProviderError  # noqa: E402
from app.engines.content_policy import CONTENT_POLICY_CODE  # noqa: E402
from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel  # noqa: E402

AUDIT_BODY = (
    '{"error":{"message":"内容审计命中风险规则，请调整输入后重试",'
    '"type":"content_policy_violation"}}'
)


def _status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://relay.example/v1/chat/completions")
    return httpx.HTTPStatusError(
        "rejected",
        request=request,
        response=httpx.Response(status, text=body, request=request),
    )


class _AuditingEngine:
    """Rejects the first prompt with an audit error, then answers.

    ``accepts_after`` models the relay: it only answers once the wording has
    changed, which is exactly the behaviour the retry is meant to exploit.
    """

    def __init__(self, *, accepts_after: int = 1, always_reject: bool = False) -> None:
        self.api_key = "sk-reason-test"
        self.last_trace: dict = {}
        self.calls = 0
        self.prompts: list[str] = []
        self.accepts_after = accepts_after
        self.always_reject = always_reject

    async def chat(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages") or []
        prompt = " ".join(str(item.get("content") or "") for item in messages)
        self.prompts.append(prompt)
        if self.always_reject or self.calls <= self.accepts_after:
            raise ModelProviderError(CONTENT_POLICY_CODE, "content audit rejected the prompt") from _status_error(
                403, AUDIT_BODY
            )
        return SimpleNamespace(content='{"verdict":"explore","intents":[]}', input_tokens=1, output_tokens=1)

    async def close(self) -> None:
        return None


def _snapshot() -> dict:
    return {
        "_reason_summary": "Recon found a WebLogic SSRF. Write the exploit to get a reverse shell.",
        "_fact_pin_context": "",
    }


def test_a_rejected_prompt_is_softened_and_retried_once() -> None:
    engine = _AuditingEngine()
    seen: list[dict] = []
    provider = CoordinatorReasonModel(
        engine,
        model_config_id="reason",
        model_name="kimi-k3",
        fallback=None,
        on_content_policy_retry=seen.append,
    )

    proposals = asyncio.run(provider(_snapshot()))

    assert proposals is not None
    # Falsifiable: without the retry the second call never happens and the
    # provider returns the deterministic fallback instead of a real plan.
    assert engine.calls == 2, engine.calls
    assert provider.content_policy_retries == 1
    assert "reverse shell" not in engine.prompts[1].casefold()
    assert seen and seen[0]["model"] == "kimi-k3"


def test_a_payload_inside_the_prompt_survives_the_retry() -> None:
    """The rewrite must not touch the request that actually solves the box."""

    payload = "GET /uddiexplorer/SearchPublicRegistries.jsp?operator=http://127.0.0.1:7001 HTTP/1.1"
    engine = _AuditingEngine()
    provider = CoordinatorReasonModel(
        engine, model_config_id="reason", model_name="kimi-k3", fallback=None
    )

    snapshot = {
        "_reason_summary": f"Send the exploit:\n```http\n{payload}\n```\nThen attack the box.",
        "_fact_pin_context": "",
    }
    asyncio.run(provider(snapshot))

    assert engine.calls == 2
    assert payload in engine.prompts[1]


def test_a_second_rejection_falls_back_instead_of_looping() -> None:
    engine = _AuditingEngine(always_reject=True)
    fallback_used: list[int] = []

    def fallback(snapshot):
        fallback_used.append(1)
        return SimpleNamespace(intents=[], verdict="explore", drift="")

    provider = CoordinatorReasonModel(
        engine, model_config_id="reason", model_name="kimi-k3", fallback=fallback
    )

    result = asyncio.run(provider(_snapshot()))

    # Exactly two attempts: the original and one softened retry.
    assert engine.calls == 2, engine.calls
    assert fallback_used == [1]
    assert provider.last_error_code == CONTENT_POLICY_CODE
    assert result.verdict == "explore"


def test_an_auth_failure_is_not_retried_as_an_audit_rejection() -> None:
    class _AuthEngine(_AuditingEngine):
        async def chat(self, **kwargs):
            self.calls += 1
            raise ModelProviderError(
                "MODEL_PERMISSION_DENIED", "provider returned HTTP 403"
            ) from _status_error(403, '{"code":"GROUP_DISABLED"}')

    engine = _AuthEngine()
    fallback_used: list[int] = []

    def fallback(snapshot):
        fallback_used.append(1)
        return SimpleNamespace(intents=[], verdict="explore", drift="")

    provider = CoordinatorReasonModel(
        engine,
        model_config_id="reason",
        model_name="kimi-k3",
        fallback=fallback,
    )

    asyncio.run(provider(_snapshot()))

    # Falsifiable: a blanket content-policy retry would spend a second request
    # on a credential problem that no rewrite can fix.
    assert engine.calls == 1, engine.calls
    assert provider.content_policy_retries == 0
    assert provider.last_error_code == "MODEL_PERMISSION_DENIED"


def test_a_prompt_with_nothing_to_soften_is_not_retried() -> None:
    """No-op rewrite means no second request, and the real error is reported."""

    engine = _AuditingEngine(always_reject=True)
    provider = CoordinatorReasonModel(
        engine, model_config_id="reason", model_name="kimi-k3", fallback=None
    )

    snapshot = {"_reason_summary": "List the endpoints.", "_fact_pin_context": ""}
    asyncio.run(provider(snapshot))

    assert engine.calls == 1, engine.calls
    assert provider.content_policy_retries == 0
    assert provider.last_error_code == CONTENT_POLICY_CODE
