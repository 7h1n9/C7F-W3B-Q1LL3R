import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(prefix="/codex-bridge", tags=["codex-bridge"])


def _failure(code: str, message: str, details: dict | None = None) -> dict:
    return {"ok": False, "code": code, "message": message, "details": details or {}}


@router.post("/self-test")
async def self_test_codex_bridge() -> dict:
    """Run a bounded Codex Bridge connectivity test.

    The probe uses an isolated temporary workspace and a fixed prompt that must
    not access any target, runner, backend-management endpoint, or local source
    tree. It is meant for diagnosing the bridge chain only.
    """
    bridge_url = get_settings().codex_bridge_url.rstrip("/")
    result: dict = {
        "bridge_url": bridge_url,
        "checks": {},
        "thread_id": None,
        "events_seen": [],
    }
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            health = await client.get(f"{bridge_url}/health")
            health.raise_for_status()
            result["checks"]["bridge_health"] = health.json()
    except httpx.ConnectError as error:
        return {"data": _failure("BRIDGE_UNREACHABLE", str(error), result)}
    except httpx.TimeoutException as error:
        return {"data": _failure("BRIDGE_TIMEOUT", str(error), result)}
    except (httpx.HTTPError, ValueError) as error:
        return {"data": _failure("BRIDGE_UNREACHABLE", str(error), result)}

    workspace = Path(tempfile.mkdtemp(prefix="c7f-codex-bridge-self-test-"))
    (workspace / "notes").mkdir(parents=True, exist_ok=True)
    (workspace / "notes" / "SELF_TEST.md").write_text(
        "Codex Bridge self-test workspace. Do not access any external target.\n",
        encoding="utf-8",
    )
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            try:
                created = await client.post(
                    f"{bridge_url}/threads",
                    json={
                        "run_id": "codex-bridge-self-test",
                        "workspace_path": str(workspace),
                        "prompt": "只回复一句：Codex Bridge self-test ok。不要调用工具，不要访问网络。",
                    },
                )
                created.raise_for_status()
                payload = created.json()
            except httpx.HTTPStatusError as error:
                code = (
                    "CODEX_AUTH_FAILED"
                    if error.response.status_code in {401, 403}
                    else "THREAD_CREATE_FAILED"
                )
                return {"data": _failure(code, str(error), result)}
            except (httpx.HTTPError, ValueError) as error:
                return {"data": _failure("THREAD_CREATE_FAILED", str(error), result)}

            thread_id = payload.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                return {"data": _failure("THREAD_CREATE_FAILED", "Bridge did not return thread_id.", result)}
            result["thread_id"] = thread_id

            try:
                async with client.stream(
                    "POST",
                    f"{bridge_url}/threads/{thread_id}/run",
                    json={"prompt": "只回复一句：Codex Bridge self-test ok。不要调用工具，不要访问网络。"},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        result["events_seen"].append(line[:500])
            except httpx.TimeoutException as error:
                return {"data": _failure("BRIDGE_TIMEOUT", str(error), result)}
            except httpx.HTTPStatusError as error:
                code = (
                    "CODEX_AUTH_FAILED"
                    if error.response.status_code in {401, 403}
                    else "EVENT_STREAM_FAILED"
                )
                return {"data": _failure(code, str(error), result)}
            except httpx.HTTPError as error:
                return {"data": _failure("EVENT_STREAM_FAILED", str(error), result)}

    except Exception as error:
        return {"data": _failure("EVENT_PARSE_FAILED", str(error), result)}

    has_agent_event = any('"agent.message"' in item for item in result["events_seen"])
    has_completed_event = any('"Codex' in item or "self-test" in item for item in result["events_seen"])
    if not has_agent_event:
        return {"data": _failure("EVENT_SEQUENCE_INVALID", "No agent.message event was observed.", result)}
    if not has_completed_event:
        return {"data": _failure("EVENT_SEQUENCE_INVALID", "No structured completion signal was observed.", result)}
    return {"data": {"ok": True, "code": "OK", "message": "Codex Bridge self-test passed.", "details": result}}
