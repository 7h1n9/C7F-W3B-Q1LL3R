from fastapi import APIRouter

from app.api.v1 import (
    challenges,
    conversations,
    events,
    health,
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
router.include_router(skills.router)
router.include_router(conversations.router)
router.include_router(runs.router)
router.include_router(events.router)
router.include_router(runner.router)
router.include_router(models.router)
router.include_router(system_settings.router)
router.include_router(readiness.router)
