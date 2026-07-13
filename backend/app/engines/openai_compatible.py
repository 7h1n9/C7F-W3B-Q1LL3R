import asyncio
import json
import time
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from app.engines.retry import TRANSIENT_HTTP_STATUSES, backoff_delay, parse_retry_after
from app.schemas.agent import AgentAction

action_adapter = TypeAdapter(AgentAction)
ACTION_SCHEMA = action_adapter.json_schema()


class ModelRateLimitError(RuntimeError):
    """The provider rejected the request because its quota/rate limit was hit."""


class ModelUnavailableError(RuntimeError):
    """The provider could not be reached or returned a transient server error."""


class OpenAICompatibleEngine:
    """Provider-neutral, structured-action client. Database and tools stay outside."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        *,
        action_protocol: str = "json_schema",
        max_output_tokens: int = 2048,
        temperature: float = 0.0,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        rate_limit_cooldown_seconds: int = 60,
    ) -> None:
        self.base_url, self.api_key, self.model, self.timeout = (
            base_url.rstrip("/"),
            api_key,
            model,
            timeout,
            )
        self.action_protocol = action_protocol
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.rate_limit_cooldown_seconds = max(1, rate_limit_cooldown_seconds)
        self.cooldown_until = 0.0
        self.last_trace: dict[str, Any] = {}
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            self._client = None

    @staticmethod
    def _extract_json(content: str) -> dict:
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ValueError("model response did not contain one JSON object")

    async def next_action(self, messages: list[dict]) -> AgentAction:
        if time.monotonic() < self.cooldown_until:
            raise ModelRateLimitError("MODEL_RATE_LIMITED: provider cooldown is active")
        repair_messages = list(messages)
        last_error = ""
        parse_attempts = 0
        network_attempts = 0
        started = time.perf_counter()
        max_attempts = max(1, self.max_retries + 1)
        formats = [
            {
                "type": "json_schema",
                "json_schema": {"name": "agent_action", "strict": True, "schema": ACTION_SCHEMA},
            },
            {"type": "json_object"},
            None,
        ]
        for attempt in range(max_attempts + 2):
            try:
                payload = {"model": self.model, "messages": repair_messages, "temperature": self.temperature, "max_tokens": self.max_output_tokens}
                protocol_index = {"json_schema": 0, "json_object": 1, "prompt_json": 2, "native_tool_call": 0}.get(self.action_protocol, 0)
                response_format = formats[protocol_index] if attempt == 0 and protocol_index < len(formats) else None
                if response_format is not None:
                    payload["response_format"] = response_format
                else:
                    payload["messages"] = repair_messages + [
                        {
                            "role": "system",
                            "content": "Return only a valid JSON AgentAction object.",
                        }
                    ]
                response = await self._get_client().post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"].get("content")
                if not isinstance(content, str):
                    raise ValueError("model response did not contain JSON content")
                action = action_adapter.validate_python(self._extract_json(content))
                usage = body.get("usage") or {}
                self.last_trace = {
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
                    "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
                    "provider_request_id": getattr(response, "headers", {}).get("x-request-id") if getattr(response, "headers", None) else None,
                    "parse_attempts": parse_attempts + 1,
                    "response_excerpt": content[:2000],
                    "action": action.model_dump(),
                }
                return action
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status == 429:
                    network_attempts += 1
                    if network_attempts <= self.max_retries:
                        delay = backoff_delay(
                            network_attempts - 1,
                            retry_after=parse_retry_after(error.response.headers),
                            base=1.0,
                            cap=15.0,
                        )
                        await asyncio.sleep(delay)
                        continue
                    self.cooldown_until = time.monotonic() + self.rate_limit_cooldown_seconds
                    raise ModelRateLimitError(
                        "MODEL_RATE_LIMITED: provider returned HTTP 429 Too Many Requests"
                    ) from error
                if status in TRANSIENT_HTTP_STATUSES:
                    network_attempts += 1
                    if network_attempts <= self.max_retries:
                        delay = backoff_delay(network_attempts - 1, base=self.retry_base_seconds, cap=15.0)
                        await asyncio.sleep(delay)
                        continue
                    raise ModelUnavailableError(
                        f"MODEL_UNAVAILABLE: provider returned HTTP {status}"
                    ) from error
                last_error = f"provider returned HTTP {status}: {error}"
                if network_attempts >= self.max_retries:
                    break
                continue
            except httpx.RequestError as error:
                network_attempts += 1
                if network_attempts <= self.max_retries:
                    await asyncio.sleep(backoff_delay(network_attempts - 1, base=self.retry_base_seconds, cap=15.0))
                    continue
                raise ModelUnavailableError(
                    f"MODEL_UNAVAILABLE: {error}"
                ) from error
            except (KeyError, ValueError, json.JSONDecodeError, ValidationError) as error:
                last_error = str(error)
                parse_attempts += 1
                if parse_attempts >= 3:
                    break
                repair_messages += [
                    {"role": "assistant", "content": "The previous output was invalid."},
                    {
                        "role": "user",
                        "content": f"Return only a valid AgentAction JSON matching the schema. Error: {last_error}",
                    },
                ]
                await asyncio.sleep(backoff_delay(parse_attempts - 1, base=0.4, cap=4.0))
        self.last_trace = {"latency_ms": round((time.perf_counter() - started) * 1000), "parse_attempts": parse_attempts, "parse_error_code": "AGENT_ACTION_PARSE_FAILED", "response_excerpt": last_error[:2000]}
        raise RuntimeError(f"AGENT_ACTION_PARSE_FAILED: {last_error}")
