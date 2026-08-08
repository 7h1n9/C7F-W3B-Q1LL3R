from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.models.skill import Skill
from app.services.skill_selection import allowed_tools_for


async def effective_tools_for(session: AsyncSession, run: SolveRun, challenge: Challenge) -> set[str]:
    # Once an Attempt manifest exists it is the immutable source of truth for
    # the running build.  Recomputing an ad-hoc intersection per request was
    # the source of runtime tool drift.
    from app.models.run import AttemptToolManifest, RunExecutionLease
    lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    asset_mysql = challenge.metadata_json.get("adapter") == "asset_warranty" and str(challenge.metadata_json.get("dbms") or "").lower() == "mysql"
    if lease and run.engine_type == "codex_sdk":
        manifest = await session.scalar(select(AttemptToolManifest).where(AttemptToolManifest.attempt_id == lease.attempt_id))
        if manifest is not None:
            effective = set(manifest.effective_tools or [])
            if asset_mysql:
                effective.discard("sqlite_metadata_discovery")
                if "mysql_metadata_discovery" in set(manifest.backend_registry_tools or []) and "mysql_metadata_discovery" in set(manifest.runner_capability_tools or []):
                    effective.add("mysql_metadata_discovery")
                if (
                    getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2"
                    and "sqlite_metadata_discovery" in set(manifest.backend_registry_tools or [])
                    and "sqlite_metadata_discovery" in set(manifest.runner_capability_tools or [])
                ):
                    effective.add("sqlite_metadata_discovery")
                if (
                    getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2"
                    and "script_run" in set(manifest.backend_registry_tools or [])
                    and "script_run" in set(manifest.runner_capability_tools or [])
                    and "script_run" in set((run.role_snapshot_json or {}).get("tools") or [])
                ):
                    effective.add("script_run")
            return effective - await forbidden_tools_for(session, run.id)
    allowed = set(allowed_tools_for(challenge.challenge_type, challenge.metadata_json or {}))
    role_tools = set((run.role_snapshot_json or {}).get("tools") or [])
    if asset_mysql:
        role_tools.discard("sqlite_metadata_discovery")
        role_tools.add("mysql_metadata_discovery")
    if role_tools:
        allowed &= role_tools
    if (
        getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2"
        and "sqlite_metadata_discovery" in allowed_tools_for(challenge.challenge_type, {})
    ):
        allowed.add("sqlite_metadata_discovery")
    if getattr(run, "solver_mode", "multi_agent_v1") == "solver_v2" and "script_run" in role_tools:
        allowed.add("script_run")
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
        # codex attempts fail closed because their catalog must be reproducible.
        if run.engine_type == "codex_sdk":
            return set()
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
