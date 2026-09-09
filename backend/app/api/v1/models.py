import json
import os
import shutil
import subprocess
import time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.model_config import ModelConfig
from app.schemas.model_config import ModelConfigUpdate, ModelConfigWrite
from app.services.crypto import decrypt_api_key, encrypt_api_key

router = APIRouter(prefix="/model-configs", tags=["settings"])


def read(item: ModelConfig) -> dict:
    capabilities = item.capabilities_json or {}
    roles = capabilities.get("roles") if isinstance(capabilities, dict) else None
    if not isinstance(roles, list) or not roles:
        roles = ["worker"]
    return {
        "id": item.id,
        "name": item.name,
        "provider_type": item.provider_type,
        "base_url": item.base_url,
        "wire_api": (
            (item.capabilities_json or {}).get("wire_api")
            or ("responses" if item.provider_type == "codex_cli" and item.base_url else None)
        ),
        "model_name": item.model_name,
        "reasoning_effort": (
            capabilities.get("reasoning_effort", "medium")
            if item.provider_type == "codex_cli"
            else None
        ),
        "enabled": item.enabled,
        "api_key_configured": bool(item.encrypted_api_key),
        "action_protocol": item.action_protocol,
        "structured_output_mode": item.structured_output_mode,
        "supports_json_schema": item.supports_json_schema,
        "supports_json_object": item.supports_json_object,
        "supports_native_tool_call": item.supports_native_tool_call,
        "request_timeout_seconds": item.request_timeout_seconds,
        "max_output_tokens": item.max_output_tokens,
        "temperature": item.temperature,
        "max_retries": item.max_retries,
        "retry_base_seconds": item.retry_base_seconds,
        "rate_limit_cooldown_seconds": item.rate_limit_cooldown_seconds,
        "requests_per_minute": item.requests_per_minute,
        "max_concurrency": item.max_concurrency,
        "context_token_limit": item.context_token_limit,
        "last_test_at": item.last_test_at.isoformat() if item.last_test_at else None,
        "last_test_ok": item.last_test_ok,
        "capabilities": capabilities,
        "roles": [str(role) for role in roles if str(role) in {"worker", "coordinator_reason"}],
    }


@router.get("")
async def list_model_configs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(ModelConfig).order_by(ModelConfig.name))).all())
    return {"data": [read(item) for item in items]}


@router.post("", status_code=201)
async def create_model_config(
    payload: ModelConfigWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    capabilities = {"roles": list(payload.roles)}
    if payload.provider_type == "codex_cli":
        capabilities["reasoning_effort"] = payload.reasoning_effort or "medium"
        if payload.base_url:
            capabilities["wire_api"] = payload.wire_api or "responses"
    item = ModelConfig(
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=str(payload.base_url).rstrip("/") if payload.base_url else None,
        model_name=payload.model_name,
        encrypted_api_key=encrypt_api_key(payload.api_key or "") if (payload.provider_type == "openai_compatible" or (payload.provider_type == "codex_cli" and payload.base_url)) else None,
        enabled=payload.enabled,
        capabilities_json=capabilities,
        **payload.model_dump(
            exclude={"name", "provider_type", "base_url", "wire_api", "model_name", "api_key", "enabled", "roles", "reasoning_effort"}
        ),
    )
    session.add(item)
    await session.commit()
    return {"data": read(item)}


@router.put("/{config_id}")
async def update_model_config(
    config_id: str, payload: ModelConfigUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    was_codex_cli = item.provider_type == "codex_cli"
    item.name, item.provider_type, item.base_url, item.model_name, item.enabled = (
        payload.name,
        payload.provider_type,
        str(payload.base_url).rstrip("/") if payload.base_url else None,
        payload.model_name,
        payload.enabled,
    )
    capabilities = dict(item.capabilities_json or {})
    capabilities["roles"] = list(payload.roles)
    if payload.provider_type == "codex_cli":
        capabilities["reasoning_effort"] = payload.reasoning_effort or "medium"
        if payload.base_url:
            capabilities["wire_api"] = payload.wire_api or "responses"
        else:
            capabilities.pop("wire_api", None)
    else:
        capabilities.pop("reasoning_effort", None)
        capabilities.pop("wire_api", None)
    item.capabilities_json = capabilities
    for key, value in payload.model_dump(
        exclude={"name", "provider_type", "base_url", "wire_api", "model_name", "api_key", "enabled", "roles", "reasoning_effort"}
    ).items():
        setattr(item, key, value)
    if payload.provider_type == "codex_cli" and not payload.base_url:
        item.encrypted_api_key = None
    elif payload.api_key:
        item.encrypted_api_key = encrypt_api_key(payload.api_key)
    elif payload.provider_type == "codex_cli" and (not was_codex_cli or not item.encrypted_api_key):
        item.encrypted_api_key = None
    await session.commit()
    return {"data": read(item)}


@router.delete("/{config_id}", status_code=204)
async def delete_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> None:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    await session.delete(item)
    await session.commit()


@router.post("/{config_id}/test")
async def test_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        from app.core.exceptions import DomainError

        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )

    started = time.perf_counter()
    configured_roles = read(item)["roles"]
    capabilities: dict[str, object] = {
        "reachable": False,
        "normal_chat": False,
        "json_object": False,
        "json_schema": False,
        "native_tool_call": False,
        "agent_action": False,
        "tool_action": False,
        "finish_action": False,
        "recommended_protocol": item.action_protocol,
        "skill_action": False,
        "max_output_tokens": item.max_output_tokens,
        "latency": None,
        "usage": {},
        "quota_state": "unknown",
        "rate_limit_state": "unknown",
        "retry_after": None,
        "roles": configured_roles,
    }
    if item.provider_type == "codex_cli":
        capabilities["model"] = str(item.model_name or "")
        capabilities["reasoning_effort"] = (item.capabilities_json or {}).get("reasoning_effort", "medium")
        if item.base_url:
            from muteki.solver.cli_driver import driver_for

            api_key = decrypt_api_key(item.encrypted_api_key) if item.encrypted_api_key else ""
            profile = {
                "engine": "codex",
                "transport": "codex_cli",
                "credential_mode": "api",
                "base_url": item.base_url,
                "wire_api": (item.capabilities_json or {}).get("wire_api") or "responses",
                "model": item.model_name or "",
                "reasoning_effort": (item.capabilities_json or {}).get("reasoning_effort") or "medium",
            }
            probe_env = dict(os.environ)
            probe_env["OPENAI_API_KEY"] = api_key
            ok, detail = driver_for(profile).health_detail(env=probe_env)
            capabilities.update(
                {
                    "reachable": ok,
                    "normal_chat": ok,
                    "cli_available": ok,
                    "wire_api": str(profile["wire_api"]),
                    "error_code": None if ok else "CODEX_CLI_ENDPOINT_UNAVAILABLE",
                    "error": "" if ok else (detail or "Codex API endpoint probe failed")[:400],
                    "latency": round((time.perf_counter() - started) * 1000),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "recommended_protocol": "codex_cli",
                }
            )
            item.capabilities_json = capabilities
            item.supports_json_schema = False
            item.supports_json_object = False
            item.supports_native_tool_call = True
            item.last_test_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
            item.last_test_ok = ok
            await session.commit()
            return {"data": {"ok": ok, "message": "Codex API（Responses）能力测试完成" if ok else "Codex API 测试失败", "capabilities": capabilities}}
        executable = os.environ.get("MUTEKI_CODEX_BIN", "").strip() or shutil.which("codex")
        version = ""
        error = ""
        if executable:
            try:
                completed = subprocess.run(
                    [executable, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                version = (completed.stdout or completed.stderr or "").strip()[-300:]
                capabilities["cli_available"] = completed.returncode == 0
                if completed.returncode != 0:
                    error = version or f"codex exited with {completed.returncode}"
            except (OSError, subprocess.SubprocessError) as exc:
                error = type(exc).__name__
        else:
            error = "codex executable not found"
        capabilities.update(
            {
                "reachable": bool(executable and capabilities.get("cli_available")),
                "normal_chat": bool(executable and capabilities.get("cli_available")),
                "cli_executable": executable or "",
                "cli_version": version,
                "model": item.model_name,
                "reasoning_effort": (item.capabilities_json or {}).get("reasoning_effort", "medium"),
                "error_code": None if executable and capabilities.get("cli_available") else "CODEX_CLI_UNAVAILABLE",
                "error": error,
                "latency": round((time.perf_counter() - started) * 1000),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "recommended_protocol": "codex_cli",
            }
        )
        item.capabilities_json = capabilities
        item.supports_json_schema = False
        item.supports_json_object = False
        item.supports_native_tool_call = True
        item.last_test_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
        item.last_test_ok = bool(capabilities["reachable"])
        await session.commit()
        return {"data": {"ok": item.last_test_ok, "message": "Codex CLI 能力测试完成", "capabilities": capabilities}}
    headers = {"Authorization": f"Bearer {decrypt_api_key(item.encrypted_api_key)}"}
    try:
        async with httpx.AsyncClient(timeout=item.request_timeout_seconds, trust_env=False) as client:
            base_payload = {
                "model": item.model_name,
                "messages": [{"role": "user", "content": "Reply with a short health response."}],
                "max_tokens": min(item.max_output_tokens, 32),
                "temperature": item.temperature,
            }
            response = await client.post(f"{item.base_url.rstrip('/')}/chat/completions", headers=headers, json=base_payload)
            response.raise_for_status()
            capabilities["reachable"] = capabilities["normal_chat"] = True
            body = response.json()
            capabilities["usage"] = body.get("usage", {})
            capabilities["latency"] = round((time.perf_counter() - started) * 1000)
            for mode, fmt in (
                ("json_object", {"type": "json_object"}),
                ("json_schema", {"type": "json_schema", "json_schema": {"name": "probe", "strict": True, "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}),
            ):
                try:
                    check = await client.post(f"{item.base_url.rstrip('/')}/chat/completions", headers=headers, json={**base_payload, "messages": [{"role": "user", "content": 'Return {"ok":true} as JSON.'}], "response_format": fmt})
                    check.raise_for_status()
                    capabilities[mode] = True
                except httpx.HTTPError:
                    capabilities[mode] = False
            for probe_name, probe in (
                ("agent_action", {"type": "finish", "result": "unsolved", "summary": "capability probe"}),
                ("finish_action", {"type": "finish", "result": "unsolved", "summary": "capability probe"}),
                ("tool_action", {"type": "tool", "phase": "INTAKE", "objective": "capability probe", "tool_name": "file_read", "arguments": {"path": "notes/probe.txt"}, "reason": "capability probe"}),
                ("skill_action", {"type": "skill", "operation": "inspect", "phase": "INTAKE", "objective": "capability probe", "reason": "capability probe", "expected_use": "capability probe"}),
            ):
                try:
                    probe_response = await client.post(
                        f"{item.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json={**base_payload, "messages": [{"role": "user", "content": json.dumps(probe)}], "response_format": {"type": "json_object"}},
                    )
                    probe_response.raise_for_status()
                    capabilities[probe_name] = True
                except httpx.HTTPStatusError as probe_error:
                    if probe_error.response.status_code == 429:
                        capabilities["retry_after"] = probe_error.response.headers.get("Retry-After")
                        capabilities["rate_limit_state"] = "limited"
                except httpx.HTTPError:
                    pass
    except httpx.HTTPStatusError as error:
        capabilities["error_code"] = {400: "MODEL_BAD_REQUEST", 401: "MODEL_AUTH_FAILED", 402: "MODEL_QUOTA_EXCEEDED", 403: "MODEL_PERMISSION_DENIED", 429: "MODEL_RATE_LIMITED"}.get(error.response.status_code, "MODEL_UNAVAILABLE")
        capabilities["quota_state"] = "exceeded" if error.response.status_code == 402 else capabilities["quota_state"]
        capabilities["rate_limit_state"] = "limited" if error.response.status_code == 429 else capabilities["rate_limit_state"]
        capabilities["error"] = str(error)
    except (httpx.HTTPError, ValueError) as error:
        capabilities["error_code"] = "MODEL_UNAVAILABLE"
        capabilities["error"] = str(error)
    capabilities["latency_ms"] = round((time.perf_counter() - started) * 1000)
    if capabilities.get("json_schema"):
        capabilities["recommended_protocol"] = "json_schema"
    elif capabilities.get("json_object"):
        capabilities["recommended_protocol"] = "json_object"
    else:
        capabilities["recommended_protocol"] = "prompt_json"
    item.capabilities_json = capabilities
    item.supports_json_schema = bool(capabilities.get("json_schema"))
    item.supports_json_object = bool(capabilities.get("json_object"))
    item.supports_native_tool_call = bool(capabilities.get("native_tool_call"))
    item.last_test_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
    item.last_test_ok = bool(capabilities["reachable"] and capabilities["normal_chat"])
    await session.commit()
    return {"data": {"ok": item.last_test_ok, "message": "模型能力测试完成", "capabilities": capabilities}}
