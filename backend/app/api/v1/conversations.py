from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.challenges import require_challenge
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.conversation import (
    ChallengeConversation,
    ChallengeConversationSkill,
    ChallengeMessage,
)
from app.models.model_config import ModelConfig
from app.models.skill import Skill
from app.schemas.conversation import (
    ConversationCreate,
    ConversationMessageCreate,
    ConversationRunCreate,
)
from app.services.chat import chat_service

router = APIRouter(tags=["conversations"])


def read(conversation: ChallengeConversation) -> dict:
    return {
        "id": conversation.id,
        "challenge_id": conversation.challenge_id,
        "model_config_id": conversation.model_config_id,
        "title": conversation.title,
        "status": conversation.status,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def message_read(message: ChallengeMessage) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "usage_json": message.usage_json,
        "error_code": message.error_code,
        "error_message": message.error_message,
        "created_at": message.created_at.isoformat(),
    }


async def require_conversation(
    conversation_id: str, session: AsyncSession
) -> ChallengeConversation:
    conversation = await session.get(ChallengeConversation, conversation_id)
    if not conversation:
        raise DomainError("CONVERSATION_NOT_FOUND", "Conversation not found.", status_code=404)
    return conversation


@router.get("/challenges/{challenge_id}/conversations")
async def list_conversations(
    challenge_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    await require_challenge(challenge_id, session)
    values = list(
        (
            await session.scalars(
                select(ChallengeConversation)
                .where(ChallengeConversation.challenge_id == challenge_id)
                .order_by(ChallengeConversation.updated_at.desc())
            )
        ).all()
    )
    return {"data": [read(value) for value in values]}


@router.post("/challenges/{challenge_id}/conversations", status_code=201)
async def create_conversation(
    challenge_id: str, payload: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    await require_challenge(challenge_id, session)
    if payload.model_config_id:
        model = await session.get(ModelConfig, payload.model_config_id)
        if not model or not model.enabled:
            raise DomainError(
                "MODEL_CONFIG_UNAVAILABLE",
                "The selected model configuration is unavailable.",
                status_code=422,
            )
    conversation = ChallengeConversation(
        challenge_id=challenge_id, model_config_id=payload.model_config_id, title=payload.title
    )
    session.add(conversation)
    await session.flush()
    for index, skill_id in enumerate(dict.fromkeys(payload.skill_ids)):
        if not await session.get(Skill, skill_id):
            raise DomainError("SKILL_NOT_FOUND", "Skill not found.", status_code=422)
        session.add(
            ChallengeConversationSkill(
                conversation_id=conversation.id, skill_id=skill_id, priority=index
            )
        )
    await session.commit()
    await session.refresh(conversation)
    return {"data": read(conversation)}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    conversation = await require_conversation(conversation_id, session)
    bindings = list(
        (
            await session.scalars(
                select(ChallengeConversationSkill)
                .where(ChallengeConversationSkill.conversation_id == conversation.id)
                .order_by(ChallengeConversationSkill.priority)
            )
        ).all()
    )
    return {
        "data": {
            **read(conversation),
            "skills": [{"skill_id": item.skill_id, "priority": item.priority} for item in bindings],
        }
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    conversation = await require_conversation(conversation_id, session)
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_conversation(conversation_id, session)
    messages = list(
        (
            await session.scalars(
                select(ChallengeMessage)
                .where(ChallengeMessage.conversation_id == conversation_id)
                .order_by(ChallengeMessage.created_at)
            )
        ).all()
    )
    return {"data": [message_read(message) for message in messages]}


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(
    conversation_id: str,
    payload: ConversationMessageCreate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    conversation = await require_conversation(conversation_id, session)
    message = await chat_service.reply(session, conversation, payload.content)
    return {"data": message_read(message)}


@router.post("/conversations/{conversation_id}/create-run", status_code=201)
async def create_run_from_conversation(
    conversation_id: str,
    payload: ConversationRunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    conversation = await require_conversation(conversation_id, session)
    from app.api.v1.runs import create_run

    values = payload.model_copy(update={"conversation_id": conversation.id})
    return await create_run(conversation.challenge_id, values, session)
