"""Small control channel for pause/resume/cancel and worker status.

The default transport is process-local and deterministic for tests. Set
``MUTEKI_CONTROL_REDIS_URL`` to use Redis lists for independent worker
processes or containers; Redis is imported lazily and remains optional for
the normal in-process runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ControlMessage:
    type: str
    worker_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "worker_id": self.worker_id, "payload": self.payload}, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, value: str | bytes) -> "ControlMessage":
        raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
        return cls(str(raw.get("type", "")), str(raw.get("worker_id", "")), dict(raw.get("payload") or {}))


class InMemoryControlBus:
    """Target-aware transport that does not let one worker consume another's command."""

    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], asyncio.Queue[ControlMessage]] = {}
        self._status: asyncio.Queue[ControlMessage] = asyncio.Queue()
        self._workers: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    def register(self, channel: str, worker_id: str) -> None:
        self._workers.setdefault(channel, set()).add(worker_id)

    async def publish(self, channel: str, message: ControlMessage) -> None:
        async with self._lock:
            targets = self._workers.get(channel, set()) if message.worker_id == "*" else {message.worker_id}
            if not targets:
                targets = {message.worker_id}
            queues = [self._queues.setdefault((channel, target), asyncio.Queue()) for target in targets]
        for queue in queues:
            await queue.put(message)

    async def publish_status(self, channel: str, message: ControlMessage) -> None:
        await self._status.put(message)

    async def receive(self, channel: str, worker_id: str, timeout: float = 0.0) -> ControlMessage | None:
        async with self._lock:
            queues = [self._queues.setdefault((channel, worker_id), asyncio.Queue())]
        for queue in queues:
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                continue
        if timeout <= 0:
            return None
        tasks = [asyncio.create_task(queue.get()) for queue in queues]
        done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if not done:
            return None
        return next(iter(done)).result()


class RedisControlBus:
    """Redis-list transport for cross-process and cross-container control."""

    def __init__(self, url: str) -> None:
        try:
            from redis import asyncio as redis_asyncio
        except ImportError as error:
            raise RuntimeError("MUTEKI_CONTROL_REDIS_URL requires the redis package") from error
        self._client = redis_asyncio.from_url(url, decode_responses=False)

    def _key(self, channel: str, worker_id: str) -> str:
        return f"{channel}:worker:{worker_id}"

    async def publish(self, channel: str, message: ControlMessage) -> None:
        await self._client.rpush(self._key(channel, message.worker_id), message.to_json())

    async def publish_status(self, channel: str, message: ControlMessage) -> None:
        await self._client.rpush(f"{channel}:status", message.to_json())

    async def receive(self, channel: str, worker_id: str, timeout: float = 0.0) -> ControlMessage | None:
        keys = [self._key(channel, worker_id), self._key(channel, "*")]
        if timeout <= 0:
            for key in keys:
                value = await self._client.lpop(key)
                if value is not None:
                    return ControlMessage.from_json(value)
            return None
        result = await self._client.brpop(keys, timeout=max(1, int(timeout)))
        return ControlMessage.from_json(result[1]) if result else None


_DEFAULT_BUS: InMemoryControlBus | RedisControlBus | None = None


def default_control_bus() -> InMemoryControlBus | RedisControlBus:
    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        url = os.environ.get("MUTEKI_CONTROL_REDIS_URL", "").strip()
        _DEFAULT_BUS = RedisControlBus(url) if url else InMemoryControlBus()
    return _DEFAULT_BUS


class ControlClient:
    """Worker-side control/status API."""

    def __init__(self, worker_id: str, channel: str = "muteki:control", *, transport=None) -> None:
        self.worker_id = worker_id
        self.channel = channel
        self.transport = transport or default_control_bus()
        register = getattr(self.transport, "register", None)
        if register is not None:
            register(channel, worker_id)

    async def send_heartbeat(self) -> None:
        await self.transport.publish_status(self.channel, ControlMessage("heartbeat", self.worker_id))

    async def check_control(self, *, timeout: float = 0.0) -> ControlMessage | None:
        return await self.transport.receive(self.channel, self.worker_id, timeout=timeout)

    async def report_status(self, status: dict[str, Any]) -> None:
        await self.transport.publish_status(self.channel, ControlMessage("status", self.worker_id, dict(status)))


class ControlReceiver:
    """Coordinator-side command API."""

    def __init__(self, channel: str = "muteki:control", *, transport=None) -> None:
        self.channel = channel
        self.transport = transport or default_control_bus()

    async def send_command(self, worker_id: str, command: str, payload: dict[str, Any] | None = None) -> None:
        if command not in {"pause", "resume", "cancel", "heartbeat", "status"}:
            raise ValueError(f"unsupported control command: {command}")
        await self.transport.publish(self.channel, ControlMessage(command, worker_id, dict(payload or {})))

    async def broadcast(self, command: str, payload: dict[str, Any] | None = None) -> None:
        await self.send_command("*", command, payload)


__all__ = ["ControlClient", "ControlMessage", "ControlReceiver", "InMemoryControlBus", "RedisControlBus", "default_control_bus"]
