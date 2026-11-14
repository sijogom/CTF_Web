"""유지보수 모드 API."""

from fastapi import APIRouter, Depends, HTTPException
from app.redis import redis_client
from app.api.deps import get_current_user, require_role

router = APIRouter(tags=["유지보수"])

MAINTENANCE_KEY = "maintenance_mode"


@router.get("/admin/maintenance")
async def get_maintenance_status():
    """유지보수 모드 상태 확인."""
    is_active = await redis_client.get(MAINTENANCE_KEY)
    return {"maintenance": bool(is_active)}


@router.post("/admin/maintenance")
async def toggle_maintenance(
    enable: bool,
    _role: str = Depends(require_role("admin")),
):
    """유지보수 모드 토글 (관리자 전용)."""
    if enable:
        await redis_client.set(MAINTENANCE_KEY, "1")
    else:
        await redis_client.delete(MAINTENANCE_KEY)

    return {"maintenance": enable, "message": f"유지보수 모드 {'활성화' if enable else '비활성화'}됨"}
