import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge, ChallengeAttachment
from app.models.conversation import (
    ChallengeConversation,
    ChallengeConversationSkill,
    ChallengeMessage,
)
from app.models.model_config import ModelConfig
from app.models.skill import Skill
from app.services.crypto import decrypt_api_key


class ChatService:
    history_limit = 16

    async def context(
        self, session: AsyncSession, conversation: ChallengeConversation
    ) -> list[dict]:
        challenge = await session.get(Challenge, conversation.challenge_id)
        if not challenge:
            raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
        attachments = list(
            (
                await session.scalars(
                    select(ChallengeAttachment).where(
                        ChallengeAttachment.challenge_id == challenge.id
                    )
                )
            ).all()
        )
        bindings = list(
            (
                await session.scalars(
                    select(ChallengeConversationSkill)
                    .where(ChallengeConversationSkill.conversation_id == conversation.id)
                    .order_by(ChallengeConversationSkill.priority)
                )
            ).all()
        )
        skills = []
        for binding in bindings:
            skill = await session.get(Skill, binding.skill_id)
            if skill and skill.enabled:
                skills.append(
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "allowed_tools": skill.allowed_tools,
                        "content": skill.content_markdown,
                    }
                )
        messages = list(
            (
                await session.scalars(
                    select(ChallengeMessage)
                    .where(ChallengeMessage.conversation_id == conversation.id)
                    .order_by(ChallengeMessage.created_at.desc())
                    .limit(self.history_limit)
                )
            ).all()
        )
        facts = {
            "challenge": {
                "name": challenge.name,
                "description": challenge.description,
                "challenge_type": challenge.challenge_type,
                "target_url": challenge.target_url
                if challenge.challenge_type == "WEB_TARGET"
                else None,
                "attachments": [
                    {
                        "name": item.original_name,
                        "kind": item.kind,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in attachments
                ],
            },
            "skills": skills,
        }
        system = "You are in authorized CTF discussion mode. Do not execute tools, access URLs, request shell commands, run code, or claim to have verified a flag. Give concise reasoning from only the supplied metadata, selected Skills, and conversation history."
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": json.dumps(facts, ensure_ascii=False)},
            *[
                {"role": message.role, "content": message.content}
                for message in reversed(messages)
                if message.role in {"user", "assistant", "system"}
            ],
        ]

    async def reply(
        self, session: AsyncSession, conversation: ChallengeConversation, content: str
    ) -> ChallengeMessage:
        if not conversation.model_config_id:
            raise DomainError(
                "CHAT_MODEL_REQUIRED",
                "Choose a model configuration before sending a message.",
                status_code=422,
            )
        config = await session.get(ModelConfig, conversation.model_config_id)
        if not config or not config.enabled or not config.base_url or not config.model_name:
            raise DomainError(
                "CHAT_MODEL_UNAVAILABLE",
                "The selected model configuration is unavailable.",
                status_code=422,
            )
        user = ChallengeMessage(
            conversation_id=conversation.id, role="user", content=content, status="COMPLETED"
        )
        session.add(user)
        await session.commit()
        assistant = ChallengeMessage(
            conversation_id=conversation.id, role="assistant", content="", status="GENERATING"
        )
        session.add(assistant)
        await session.commit()
        await session.refresh(assistant)
        try:
            messages = await self.context(session, conversation)
            async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
                response = await client.post(
                    f"{config.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {decrypt_api_key(config.encrypted_api_key)}"
                    },
                    json={"model": config.model_name, "messages": messages, "temperature": 0.2},
                )
                response.raise_for_status()
            body = response.json()
            assistant.content = str(body["choices"][0]["message"]["content"])
            assistant.usage_json = body.get("usage") or {}
            assistant.status = "COMPLETED"
        except (httpx.HTTPError, KeyError, ValueError, DomainError) as error:
            assistant.status, assistant.error_code, assistant.error_message = (
                "FAILED",
                "CHAT_MODEL_ERROR",
                str(error)[:1000],
            )
        await session.commit()
        await session.refresh(assistant)
        conversation.updated_at = datetime.now(UTC)
        await session.commit()
        return assistant


chat_service = ChatService()
