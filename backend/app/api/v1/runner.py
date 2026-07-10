from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.runs import require_run
from app.core.database import get_session
from app.models.challenge import Challenge
from app.schemas.tool import ToolInvoke
from app.tools.gateway import tool_gateway

router = APIRouter(prefix="/runs", tags=["tools"])


@router.post("/{run_id}/tools")
async def invoke_tool(run_id: str, payload: ToolInvoke, session: AsyncSession = Depends(get_session)) -> dict:
    run = await require_run(run_id, session)
    challenge = await session.get(Challenge, run.challenge_id)
    return {"data": await tool_gateway.invoke(session, run, challenge, payload.name, payload.arguments)}
