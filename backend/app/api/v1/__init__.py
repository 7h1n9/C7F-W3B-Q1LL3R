from fastapi import APIRouter

from app.api.v1 import challenges, events, health, models, runner, runs, system_settings

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(challenges.router)
router.include_router(runs.router)
router.include_router(events.router)
router.include_router(runner.router)
router.include_router(models.router)
router.include_router(system_settings.router)
