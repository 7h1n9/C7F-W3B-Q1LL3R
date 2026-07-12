import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.engines.base import EngineEvent, SolveEngine
from app.engines.retry import TRANSIENT_HTTP_STATUSES, backoff_delay, parse_retry_after

CODEX_CONTROL_PROTOCOL = (
    "\n\n[CTF CONTROL PROTOCOL]\n"
    "每次普通分析回合结束后继续推进授权范围内的解题，不要等待用户。"
    "只有确实需要用户提供信息、确认高风险动作或人工裁定时，才在最终消息末尾单独输出精确标记 "
    "[[C7F_WAITING_USER]]；没有这个标记就不要停下来。"
)


class BridgeRateLimitError(RuntimeError):
    pass


class BridgeUnavailableError(RuntimeError):
    pass


class CodexSdkEngine(SolveEngine):
    def __init__(self, bridge_url: str, workspace_path: str, thread_id: str | None = None) -> None:
        self.bridge_url, self.workspace_path = bridge_url.rstrip("/"), workspace_path
        self.thread_ids: dict[str, str] = {}
        self.max_attempts = 5
        self.initial_thread_id = thread_id

    def _thread_for(self, run_id: str) -> str | None:
        if run_id not in self.thread_ids and self.initial_thread_id:
            self.thread_ids[run_id] = self.initial_thread_id
        return self.thread_ids.get(run_id)

    async def _post_json_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict,
        *,
        timeout_label: str,
        cancel_not_supported: bool = False,
    ) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                content = getattr(response, "content", None)
                if content is not None and not content:
                    return {}
                return response.json()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                last_error = error
                if status in TRANSIENT_HTTP_STATUSES and attempt < self.max_attempts - 1:
                    await self._sleep_for_retry(
                        attempt, retry_after=parse_retry_after(error.response.headers)
                    )
                    continue
                if cancel_not_supported and status == 501:
                    raise RuntimeError("CANCEL_NOT_SUPPORTED") from error
                if status == 429:
                    raise BridgeRateLimitError(
                        f"CODEX_BRIDGE_RATE_LIMITED: bridge returned HTTP 429 during {timeout_label}"
                    ) from error
                if status in TRANSIENT_HTTP_STATUSES:
                    raise BridgeUnavailableError(
                        f"CODEX_BRIDGE_UNAVAILABLE: bridge returned HTTP {status} during {timeout_label}"
                    ) from error
                raise
            except ValueError as error:
                last_error = error
                if attempt < self.max_attempts - 1:
                    await self._sleep_for_retry(attempt)
                    continue
                raise BridgeUnavailableError(
                    f"CODEX_BRIDGE_UNAVAILABLE: invalid JSON during {timeout_label}: {error}"
                ) from error
            except httpx.RequestError as error:
                last_error = error
                if attempt < self.max_attempts - 1:
                    await self._sleep_for_retry(attempt)
                    continue
                raise BridgeUnavailableError(
                    f"CODEX_BRIDGE_UNAVAILABLE: {error}"
                ) from error
        raise BridgeUnavailableError(
            f"CODEX_BRIDGE_UNAVAILABLE: retry exhausted during {timeout_label}: {last_error}"
        )

    async def _sleep_for_retry(
        self, attempt: int, *, retry_after: float | None = None, base: float = 1.0, cap: float = 15.0
    ) -> None:
        await asyncio.sleep(backoff_delay(attempt, base=base, cap=cap, retry_after=retry_after))

    async def _stream_events(self, url: str, payload: dict) -> AsyncIterator[EngineEvent]:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            yielded_any = False
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", url, json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            yielded_any = True
                            event = json.loads(line)
                            yield EngineEvent(
                                str(event.get("type", "agent.message")),
                                dict(event.get("payload", {})),
                                event.get("status"),
                            )
                return
            except httpx.HTTPStatusError as error:
                last_error = error
                status = error.response.status_code
                if status in TRANSIENT_HTTP_STATUSES and attempt < self.max_attempts - 1:
                    await self._sleep_for_retry(
                        attempt, retry_after=parse_retry_after(error.response.headers)
                    )
                    continue
                if status == 429:
                    raise BridgeRateLimitError(
                        f"CODEX_BRIDGE_RATE_LIMITED: bridge returned HTTP 429 while streaming {url}"
                    ) from error
                if status in TRANSIENT_HTTP_STATUSES:
                    raise BridgeUnavailableError(
                        f"CODEX_BRIDGE_UNAVAILABLE: bridge returned HTTP {status} while streaming {url}"
                    ) from error
                raise
            except (httpx.RequestError, json.JSONDecodeError, ValueError) as error:
                last_error = error
                if yielded_any or attempt == self.max_attempts - 1:
                    raise BridgeUnavailableError(
                        f"CODEX_BRIDGE_UNAVAILABLE: {error}"
                    ) from error
                await self._sleep_for_retry(attempt)
        if last_error is not None:
            raise BridgeUnavailableError(f"CODEX_BRIDGE_UNAVAILABLE: {last_error}") from last_error

    async def start(self, run_id: str, prompt: str | None = None) -> AsyncIterator[EngineEvent]:
        prompt = prompt or (
            "Analyze only this authorized CTF workspace. "
            "Read the challenge description and workspace files before taking the next step."
        )
        prompt = f"{prompt}{CODEX_CONTROL_PROTOCOL}"
        async with httpx.AsyncClient(timeout=300) as client:
            payload = await self._post_json_with_retry(
                client,
                f"{self.bridge_url}/threads",
                {
                    "run_id": run_id,
                    "workspace_path": self.workspace_path,
                    "prompt": prompt,
                },
                timeout_label="thread creation",
            )
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            raise RuntimeError("Codex Bridge did not return a thread ID")
        self.thread_ids[run_id] = thread_id
        yield EngineEvent(
            "agent.message", {"message": "Codex thread created", **payload}, "ANALYZING"
        )
        async for event in self._stream_events(
            f"{self.bridge_url}/threads/{thread_id}/run",
            {"prompt": prompt},
        ):
            yield event

    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]:
        thread_id = self._thread_for(run_id)
        if not thread_id:
            async for event in self.start(run_id, message):
                yield event
            return
        async for event in self._stream_events(
            f"{self.bridge_url}/threads/{thread_id}/run",
            {"prompt": f"{message}{CODEX_CONTROL_PROTOCOL}"},
        ):
            yield event

    async def cancel(self, run_id: str) -> None:
        thread_id = self._thread_for(run_id)
        if not thread_id:
            return
        async with httpx.AsyncClient(timeout=15) as client:
            response = await self._post_json_with_retry(
                client,
                f"{self.bridge_url}/threads/{thread_id}/cancel",
                {},
                timeout_label="cancel",
                cancel_not_supported=True,
            )
            if isinstance(response, dict) and response.get("status") == "cancelled":
                return

    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]:
        thread_id = self._thread_for(run_id)
        if not thread_id:
            async for event in self.start(
                run_id,
                "Resume the authorized CTF analysis from the existing workspace. "
                "Read CONTEXT.md, TODO.md, prior evidence and artifacts before taking the next step.",
            ):
                yield event
            return
        try:
            async for event in self._stream_events(
                f"{self.bridge_url}/threads/{thread_id}/resume",
                {"prompt": f"Resume the authorized analysis.{CODEX_CONTROL_PROTOCOL}"},
            ):
                yield event
        except httpx.HTTPStatusError as error:
            # A bridge restart may discard its in-memory thread registry. Recreate
            # a thread while retaining the durable local solver state and evidence.
            if error.response.status_code != 404:
                raise
            self.thread_ids.pop(run_id, None)
            async for event in self.start(
                run_id,
                "Resume the authorized CTF analysis from the existing workspace. "
                "Read CONTEXT.md, TODO.md, prior evidence and artifacts before taking the next step.",
            ):
                yield event
