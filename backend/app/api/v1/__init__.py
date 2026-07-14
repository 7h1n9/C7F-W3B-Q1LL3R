from fastapi import APIRouter

from app.api.v1 import (
    challenges,
    codex_bridge,
    conversations,
    ctfctl,
    events,
    health,
    learned_skills,
    models,
    readiness,
    runner,
    runs,
    skills,
    system_settings,
)

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(challenges.router)
router.include_router(codex_bridge.router)
router.include_router(ctfctl.router)
router.include_router(skills.router)
router.include_router(conversations.router)
router.include_router(runs.router)
router.include_router(events.router)
router.include_router(runner.router)
router.include_router(models.router)
router.include_router(system_settings.router)
router.include_router(readiness.router)
router.include_router(learned_skills.router)
