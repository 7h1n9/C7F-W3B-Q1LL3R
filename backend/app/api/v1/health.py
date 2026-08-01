from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.core.database import engine
from app.services.runtime_build import backend_build_manifest

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"data": {"status": "ok", "build": backend_build_manifest()}}


@router.get("/health/live")
async def health_live() -> dict:
    return {"data": {"status": "ok", "live": True}}


@router.get("/health/ready")
async def health_ready() -> dict:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_READY", "message": str(error)[:500]},
        ) from error
    return {"data": {"status": "ready", "ready": True, "database": "mysql+asyncmy"}}
