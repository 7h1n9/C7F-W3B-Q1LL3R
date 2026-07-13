from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.models.skill import Skill
from app.services.skill_selection import allowed_tools_for


async def effective_tools_for(session: AsyncSession, run: SolveRun, challenge: Challenge) -> set[str]:
    allowed = set(allowed_tools_for(challenge.challenge_type))
    role_tools = set((run.role_snapshot_json or {}).get("tools") or [])
    if role_tools:
        allowed &= role_tools
    try:
        from app.services.runner_client import runner_client

        capability = await runner_client.capabilities()
        rows = capability.get("tools") if isinstance(capability, dict) else None
        if isinstance(rows, list):
            allowed &= {
                str(item.get("name"))
                for item in rows
                if isinstance(item, dict)
                and item.get("implemented", item.get("available", False))
                and item.get("installed", True)
                and item.get("enabled", True)
                and item.get("self_test_ok", True)
            }
    except Exception:
        # Keep local test/fallback engines usable when the optional Runner is down;
        # actual invocation still fails closed in ToolGateway.
        pass
    return allowed - await forbidden_tools_for(session, run.id)


async def forbidden_tools_for(session: AsyncSession, run_id: str) -> set[str]:
    from app.models.solver_state import SolverState

    state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
    if not state:
        return set()
    if not state.active_skill_ids_json:
        return set()
    skills = list((await session.scalars(select(Skill).where(Skill.id.in_(state.active_skill_ids_json)))).all())
    forbidden: set[str] = set()
    for skill in skills:
        forbidden.update(skill.forbidden_tools or [])
    return forbidden
