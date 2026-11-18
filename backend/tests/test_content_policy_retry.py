"""Content-audit rejections must be identifiable, recoverable, and non-destructive.

Three properties, each with the failure it prevents:

1. **Identify.** A 403 carrying ``content_policy_violation`` is not an auth
   failure.  Before this, every 403 became ``MODEL_PERMISSION_DENIED`` and the
   body was discarded, so the operator was pointed at a credential problem that
   did not exist.
2. **Recover.** One softened retry must actually be attempted, and only once.
3. **Non-destructive.** The rewrite must not touch payload bytes.  A payload
   silently changed by a "safety" pass is worse than no retry: the Run proceeds
   with a request that cannot work and reports it as an engine failure.
"""

from __future__ import annotations

import pytest

from app.engines.content_policy import (
    CONTENT_POLICY_CODE,
    is_content_policy_error,
    looks_like_content_policy_body,
    soften_prompt_for_audit,
)

# The exact body the relay returns, kept verbatim: this is the contract the
# detector is written against, so paraphrasing it would test nothing.
RELAY_BODY = (
    '{"error":{"message":"内容审计命中风险规则，请调整输入后重试",'
    '"type":"content_policy_violation"}}'
)


class _Wrapped(Exception):
    """Mirror the engine's wrapping: the status lives on the inner exception."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.response = None
        super().__init__(f"{code}: {message}")


class _WithResponse(Exception):
    def __init__(self, body: str, status: int = 403) -> None:
        class _Response:
            text = body
            status_code = status

        self.response = _Response()
        super().__init__(f"provider returned HTTP {status}")


# ── identification ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        RELAY_BODY,
        '{"type":"content_policy_violation"}',
        '{"error":{"type":"content_filter"}}',
        '{"error":{"message":"违反 responsible ai policy"}}',
        "内容审计命中风险规则",
    ],
)
def test_the_relay_body_is_recognised(body: str) -> None:
    assert looks_like_content_policy_body(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "",
        '{"error":{"message":"Invalid API key"}}',
        '{"code":"INVALID_API_KEY"}',
        '{"error":{"message":"model may not exist"}}',
    ],
)
def test_a_plain_auth_failure_is_not_a_content_policy_error(body: str) -> None:
    """The distinction this module exists for: 403-for-auth must stay fatal."""

    assert looks_like_content_policy_body(body) is False


def test_a_wrapped_provider_error_is_seen_through_the_chain() -> None:
    """The engine wraps the status error; the body is on the inner exception."""

    inner = _WithResponse(RELAY_BODY)
    try:
        raise _Wrapped(CONTENT_POLICY_CODE, "content audit rejected the prompt") from inner
    except _Wrapped as error:
        assert is_content_policy_error(error) is True


def test_an_auth_403_is_not_mistaken_for_an_audit_rejection() -> None:
    try:
        raise _Wrapped("MODEL_PERMISSION_DENIED", "provider returned HTTP 403")
    except _Wrapped as error:
        assert is_content_policy_error(error) is False


def test_the_body_alone_is_enough_without_the_code() -> None:
    """A relay that answers 403 with only the audit body must still be caught."""

    try:
        raise _WithResponse(RELAY_BODY)
    except _WithResponse as error:
        assert is_content_policy_error(error) is True


# ── softening: prose only ────────────────────────────────────────────────────


def test_prose_wording_is_softened() -> None:
    text = "Write the exact request to get a reverse shell, then escalate privileges."
    softened = soften_prompt_for_audit(text)
    assert "reverse shell" not in softened.casefold()
    assert "outbound connectivity check" in softened
    assert softened != text


def test_a_fenced_payload_is_byte_identical() -> None:
    """The payload IS the answer; a rewrite here breaks the Run silently."""

    payload = (
        "```http\n"
        "GET /uddiexplorer/SearchPublicRegistries.jsp?operator=http://172.16.0.2:6379/"
        "test%0D%0Aset%201%20reverse%20shell HTTP/1.1\n"
        "Host: target\n"
        "```"
    )
    text = f"Here is the exploit for the attack:\n{payload}\nDo not attack the box."
    softened = soften_prompt_for_audit(text)
    assert payload in softened


def test_an_inline_endpoint_is_untouched() -> None:
    text = "Send the exploit to `http://127.0.0.1:7001/console` and attack it."
    softened = soften_prompt_for_audit(text)
    assert "`http://127.0.0.1:7001/console`" in softened


def test_a_bare_url_is_untouched() -> None:
    text = "The exploit lives at https://github.com/vulhub/vulhub/tree/master/weblogic/ssrf"
    softened = soften_prompt_for_audit(text)
    assert "https://github.com/vulhub/vulhub/tree/master/weblogic/ssrf" in softened


def test_a_request_line_is_untouched() -> None:
    line = "POST /import.php HTTP/1.1"
    softened = soften_prompt_for_audit(f"Replay this exploit: {line}")
    assert line in softened


def test_a_clean_prompt_is_returned_unchanged() -> None:
    """A no-op must be detectable, otherwise the adapter spends a wasted retry."""

    text = "List the reachable endpoints and report their status codes."
    assert soften_prompt_for_audit(text) == text


def test_softening_is_idempotent() -> None:
    """A second pass must not keep degrading the text."""

    text = "Exploit the attack surface to get a reverse shell."
    once = soften_prompt_for_audit(text)
    twice = soften_prompt_for_audit(once)
    assert once == twice


# ── engine-level mapping: the 403 body decides the error code ────────────────


class _FakeResponse:
    def __init__(self, body: str, status: int = 403) -> None:
        self.text = body
        self.status_code = status


def test_the_engine_maps_an_audit_403_to_its_own_code() -> None:
    """The distinction has to exist at the engine, not only in the adapter.

    Otherwise the Worker path (which never goes through the Reason adapter)
    keeps reporting a credential problem for a prompt-wording problem.
    """

    from app.engines.openai_compatible import _provider_error_from_status

    mapped = _provider_error_from_status(403, _FakeResponse(RELAY_BODY))
    assert mapped is not None
    assert mapped.code == CONTENT_POLICY_CODE
    # The detail must carry the audit sentence, since that is the only clue an
    # operator gets about what to change.
    assert "内容审计" in str(mapped)


def test_the_engine_keeps_a_plain_403_as_permission_denied() -> None:
    from app.engines.openai_compatible import _provider_error_from_status

    mapped = _provider_error_from_status(403, _FakeResponse('{"code":"GROUP_DISABLED"}'))
    assert mapped is not None
    assert mapped.code == "MODEL_PERMISSION_DENIED"


def test_the_engine_keeps_a_401_as_auth_failed() -> None:
    from app.engines.openai_compatible import _provider_error_from_status

    mapped = _provider_error_from_status(401, _FakeResponse('{"code":"INVALID_API_KEY"}', 401))
    assert mapped is not None
    assert mapped.code == "MODEL_AUTH_FAILED"


def test_a_non_403_status_is_left_to_the_caller() -> None:
    from app.engines.openai_compatible import _provider_error_from_status

    assert _provider_error_from_status(400, _FakeResponse("{}", 400)) is None
    assert _provider_error_from_status(429, _FakeResponse("{}", 429)) is None
