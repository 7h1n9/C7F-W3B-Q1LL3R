"""The settings-page probe must survive a model that rejects one parameter.

A relay can accept a single ``temperature`` for a given model and answer 400 for
every other value.  Before this, such a model was reported as broken, and a
model that fails its health probe is dropped from the worker pool -- so the
endpoint was idle while the Run starved.  These tests pin the negotiation:
one retry with the value the upstream named, and no retry for a 400 that is
about something else.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app.api.v1.models import _post_probe_negotiating

_ENDPOINT = "https://relay.example/v1/chat/completions"

_CONSTRAINT_BODY = json.dumps(
    {
        "error": {
            "message": "field temperature invalid, only 1 is allowed for this model",
            "type": "invalid_request_error",
        }
    }
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sent_bodies(seen: list[bytes]) -> list[dict]:
    return [json.loads(body) for body in seen]


def _response(status_code: int, body: str) -> httpx.Response:
    request = httpx.Request("POST", _ENDPOINT)
    return httpx.Response(status_code, text=body, request=request)


def _handler(seen: list[bytes], statuses: list[int], bodies: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        index = len(seen)
        seen.append(request.content)
        return _response(statuses[index], bodies[index])

    return handle


@pytest.mark.anyio
async def test_probe_retries_with_the_value_the_upstream_named() -> None:
    seen: list[bytes] = []
    client = _client(
        _handler(
            seen,
            [400, 200],
            [_CONSTRAINT_BODY, json.dumps({"choices": [{"message": {"content": "ok"}}]})],
        )
    )
    async with client:
        response, negotiated = await _post_probe_negotiating(
            client,
            _ENDPOINT,
            headers={},
            payload={"model": "kimi-k3", "temperature": 0.0, "messages": []},
            deadline=time.monotonic() + 30,
            configured_timeout=15.0,
        )

    assert response.status_code == 200
    assert negotiated == {"temperature": 1.0}
    bodies = _sent_bodies(seen)
    assert len(bodies) == 2
    assert bodies[0]["temperature"] == 0.0
    assert bodies[1]["temperature"] == 1


@pytest.mark.anyio
async def test_probe_drops_the_field_when_no_value_is_offered() -> None:
    seen: list[bytes] = []
    body = json.dumps({"error": {"message": "field top_p invalid for this model"}})
    client = _client(
        _handler(
            seen,
            [400, 200],
            [body, json.dumps({"choices": [{"message": {"content": "ok"}}]})],
        )
    )
    async with client:
        response, negotiated = await _post_probe_negotiating(
            client,
            _ENDPOINT,
            headers={},
            payload={"model": "m", "top_p": 0.9, "messages": []},
            deadline=time.monotonic() + 30,
            configured_timeout=15.0,
        )

    assert response.status_code == 200
    assert negotiated == {"top_p": None}
    bodies = _sent_bodies(seen)
    assert bodies[0]["top_p"] == 0.9
    assert "top_p" not in bodies[1]


@pytest.mark.anyio
async def test_probe_does_not_retry_an_unrelated_400() -> None:
    seen: list[bytes] = []
    client = _client(
        _handler(seen, [400], [json.dumps({"error": {"message": "context length exceeded"}})])
    )
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await _post_probe_negotiating(
                client,
                _ENDPOINT,
                headers={},
                payload={"model": "m", "temperature": 0.0, "messages": []},
                deadline=time.monotonic() + 30,
                configured_timeout=15.0,
            )

    assert len(seen) == 1


@pytest.mark.anyio
async def test_probe_reports_no_constraints_on_the_happy_path() -> None:
    seen: list[bytes] = []
    client = _client(
        _handler(seen, [200], [json.dumps({"choices": [{"message": {"content": "ok"}}]})])
    )
    async with client:
        response, negotiated = await _post_probe_negotiating(
            client,
            _ENDPOINT,
            headers={},
            payload={"model": "m", "temperature": 0.0, "messages": []},
            deadline=time.monotonic() + 30,
            configured_timeout=15.0,
        )

    assert response.status_code == 200
    assert negotiated == {}
    assert len(seen) == 1
