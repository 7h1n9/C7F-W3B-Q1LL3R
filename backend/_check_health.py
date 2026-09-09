import asyncio, os
os.environ.setdefault("APP_DATABASE_URL", "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent")
from app.models.model_config import ModelConfig
from app.core.database import SessionLocal
from app.services.crypto import decrypt_api_key
from muteki.solver.cli_driver import driver_for

async def main():
    async with SessionLocal() as sess:
        mc = await sess.get(ModelConfig, "7dc8b94a-802d-41b4-bb14-dfdd721a25ed")
        if not mc:
            print("Model config not found")
            return
        caps = mc.capabilities_json or {}
        profile = {
            "engine": "codex",
            "transport": "codex_cli",
            "protocol": "codex_cli",
            "model": mc.model_name or mc.name,
            "reasoning_effort": caps.get("reasoning_effort", "medium"),
            "base_url": str(mc.base_url or "").strip(),
            "wire_api": caps.get("wire_api", "responses"),
            "api_key_ref": "env:OPENAI_API_KEY",
        }
        api_key = decrypt_api_key(mc.encrypted_api_key) if mc.encrypted_api_key else ""
        env = {"OPENAI_API_KEY": api_key}
        probe_env = {**os.environ, **env}
        driver = driver_for(profile)
        ok, detail = driver.health_detail(env=probe_env)
        detail_short = detail[:200] if detail else ""
        print(f"ok={ok}, detail={detail_short}")

asyncio.run(main())