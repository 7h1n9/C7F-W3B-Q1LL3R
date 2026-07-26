from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"data": {"status": "ok"}}


@router.get("/health/live")
async def health_live() -> dict:
    return {"data": {"status": "ok", "live": True}}


@router.get("/health/ready")
async def health_ready() -> dict:
    # Dependency health is checked by the readiness/preflight endpoints; this
    # endpoint remains cheap so Start-All can distinguish an accepting backend
    # from a process that has not mounted its API yet.
    return {"data": {"status": "ready", "ready": True}}
