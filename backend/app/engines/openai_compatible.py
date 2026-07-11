import asyncio
import json

import httpx
from pydantic import TypeAdapter, ValidationError

from app.schemas.agent import AgentAction

action_adapter = TypeAdapter(AgentAction)
ACTION_SCHEMA = action_adapter.json_schema()


class OpenAICompatibleEngine:
    """Provider-neutral, structured-action client. Database and tools stay outside."""
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    async def next_action(self, messages: list[dict]) -> AgentAction:
        repair_messages = list(messages)
        last_error = ""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json={"model": self.model, "messages": repair_messages, "response_format": {"type": "json_schema", "json_schema": {"name": "agent_action", "strict": True, "schema": ACTION_SCHEMA}}, "temperature": 0})
                    response.raise_for_status()
                content = response.json()["choices"][0]["message"].get("content")
                if not isinstance(content, str):
                    raise ValueError("model response did not contain JSON content")
                return action_adapter.validate_python(json.loads(content))
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError, ValidationError) as error:
                last_error = str(error)
                if attempt == 2:
                    break
                repair_messages += [{"role": "assistant", "content": "The previous output was invalid."}, {"role": "user", "content": f"Return only a valid AgentAction JSON matching the schema. Error: {last_error}"}]
                await asyncio.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"AGENT_ACTION_PARSE_FAILED: {last_error}")
