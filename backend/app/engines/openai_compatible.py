import asyncio
import json

import httpx
from pydantic import TypeAdapter, ValidationError

from app.schemas.agent import AgentAction

action_adapter = TypeAdapter(AgentAction)
ACTION_SCHEMA = action_adapter.json_schema()


class ModelRateLimitError(RuntimeError):
    """The provider rejected the request because its quota/rate limit was hit."""


class ModelUnavailableError(RuntimeError):
    """The provider could not be reached or returned a transient server error."""


class OpenAICompatibleEngine:
    """Provider-neutral, structured-action client. Database and tools stay outside."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url, self.api_key, self.model, self.timeout = (
            base_url.rstrip("/"),
            api_key,
            model,
            timeout,
        )

    async def next_action(self, messages: list[dict]) -> AgentAction:
        repair_messages = list(messages)
        last_error = ""
        formats = [
            {
                "type": "json_schema",
                "json_schema": {"name": "agent_action", "strict": True, "schema": ACTION_SCHEMA},
            },
            {"type": "json_object"},
            None,
        ]
        for attempt in range(3):
            try:
                payload = {"model": self.model, "messages": repair_messages, "temperature": 0}
                if formats[attempt] is not None:
                    payload["response_format"] = formats[attempt]
                else:
                    payload["messages"] = repair_messages + [
                        {
                            "role": "system",
                            "content": "Return only a valid JSON AgentAction object.",
                        }
                    ]
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                content = response.json()["choices"][0]["message"].get("content")
                if not isinstance(content, str):
                    raise ValueError("model response did not contain JSON content")
                return action_adapter.validate_python(json.loads(content))
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status == 429:
                    if attempt < 2:
                        retry_after = error.response.headers.get("Retry-After", "")
                        try:
                            delay = min(max(float(retry_after), 0.5), 8.0)
                        except ValueError:
                            delay = 1.0 + attempt
                        await asyncio.sleep(delay)
                        continue
                    raise ModelRateLimitError(
                        "MODEL_RATE_LIMITED: provider returned HTTP 429 Too Many Requests"
                    ) from error
                if status >= 500:
                    if attempt < 2:
                        await asyncio.sleep(1.0 + attempt)
                        continue
                    raise ModelUnavailableError(
                        f"MODEL_UNAVAILABLE: provider returned HTTP {status}"
                    ) from error
                last_error = f"provider returned HTTP {status}: {error}"
                if attempt == 2:
                    break
                continue
            except httpx.RequestError as error:
                if attempt < 2:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                raise ModelUnavailableError(
                    f"MODEL_UNAVAILABLE: {error}"
                ) from error
            except (KeyError, ValueError, json.JSONDecodeError, ValidationError) as error:
                last_error = str(error)
                if attempt == 2:
                    break
                repair_messages += [
                    {"role": "assistant", "content": "The previous output was invalid."},
                    {
                        "role": "user",
                        "content": f"Return only a valid AgentAction JSON matching the schema. Error: {last_error}",
                    },
                ]
                await asyncio.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"AGENT_ACTION_PARSE_FAILED: {last_error}")
