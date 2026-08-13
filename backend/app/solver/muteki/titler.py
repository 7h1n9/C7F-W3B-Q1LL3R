"""Muteki-compatible asynchronous run titling."""

from __future__ import annotations

import re

from sqlalchemy import select

from app.core.database import SessionLocal
from app.engines.openai_compatible import OpenAICompatibleEngine
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import SolveRun
from app.services.crypto import decrypt_api_key
from app.services.events import event_service
from app.solver.muteki.runtime.configuration import runtime_selection_from_hints

TITLE_EVENT = "muteki.run.titled"
TITLE_MODEL = "deepseek-v4-flash"
_REFUSAL_PREFIXES = ("i cannot", "i can't", "i am sorry", "i'm sorry", "sorry")


def fallback_title(prompt: str, *, max_words: int = 6, max_chars: int = 48) -> str:
    """Return a readable prompt-derived title without calling a model."""

    text = re.sub(r"\s+", " ", str(prompt or "").strip())
    if not text:
        return ""
    if " " not in text:
        return text[:max_chars]
    return " ".join(text.split(" ")[:max_words])[:max_chars]


def clean_title(raw: str, prompt: str) -> str:
    """Keep only a short one-line model title; degrade to the prompt on junk."""

    title = str(raw or "").strip().splitlines()[0].strip() if raw else ""
    title = title.strip('"\'`“”‘’「」『』 ')
    title = title.rstrip(".。!?！？ ").strip('"\'`“”‘’「」『』 ')
    if (
        not title
        or len(title) > 80
        or any(title.casefold().startswith(prefix) for prefix in _REFUSAL_PREFIXES)
    ):
        return fallback_title(prompt)
    return title


def _stored_title(run: SolveRun) -> str:
    value = (run.hints_json or {}).get("muteki_title")
    return str(value).strip() if value else ""


def _prompt(run: SolveRun, challenge: Challenge) -> str:
    opening = str(run.conversation_summary or "").strip()
    if opening:
        return opening[:2000]
    return "\n".join(
        item
        for item in (str(challenge.name or "").strip(), str(challenge.description or "").strip())
        if item
    )[:2000]


async def generate_run_title(run_id: str) -> str:
    """Persist a presentation-only title without affecting solver execution."""

    async with SessionLocal() as session:
        run = await session.get(SolveRun, run_id)
        if run is None:
            return ""
        existing = _stored_title(run)
        if existing:
            return existing
        challenge = await session.get(Challenge, run.challenge_id)
        if challenge is None:
            return ""
        prompt = _prompt(run, challenge)
        title = fallback_title(prompt) or str(challenge.name or "Muteki Run")[:48]
        engine: OpenAICompatibleEngine | None = None
        used_llm = False
        try:
            reason_id, _ = runtime_selection_from_hints(
                run.hints_json,
                fallback_engine_type=run.engine_type,
                fallback_model_config_id=run.model_config_id,
            )
            config = await session.get(ModelConfig, reason_id) if reason_id else None
            if config is None or not config.enabled:
                config = await session.scalar(
                    select(ModelConfig).where(ModelConfig.enabled).order_by(ModelConfig.updated_at.desc())
                )
            if config is not None and config.base_url and config.encrypted_api_key:
                engine = OpenAICompatibleEngine(
                    config.base_url,
                    decrypt_api_key(config.encrypted_api_key),
                    str(config.model_name or TITLE_MODEL),
                    timeout=min(10.0, max(1.0, float(config.request_timeout_seconds or 10))),
                    max_output_tokens=64,
                    temperature=0.3,
                    max_retries=0,
                )
                response = await engine.chat(
                    model=str(config.model_name or TITLE_MODEL),
                    messages=[
                        {"role": "system", "content": "Name this solve conversation in 3 to 6 words. Use the same language as the input. Return only the title."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=64,
                )
                title = clean_title(response.content, prompt) or title
                used_llm = True
        except Exception:
            pass
        finally:
            if engine is not None:
                await engine.close()

        hints = dict(run.hints_json or {})
        hints["muteki_title"] = title
        run.hints_json = hints
        await session.commit()
        await event_service.append(session, run_id, TITLE_EVENT, {"title": title, "source": "llm" if used_llm else "fallback"})
        return title


__all__ = ["TITLE_EVENT", "clean_title", "fallback_title", "generate_run_title"]
