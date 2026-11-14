from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(tags=["활동"])

# 공개 피드에 노출할 액션 타입
PUBLIC_ACTIONS = {
    "LOGIN_SUCCESS", "USER_REGISTERED", "CHALLENGE_SOLVED",
    "COMPETITION_JOINED", "TEAM_CREATED", "POST_CREATED",
}


@router.get("/activities")
async def get_activities(
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """전체 활동 피드 — 최근 공개 활동 목록을 반환."""
    result = await db.execute(
        select(AuditLog, User.username)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .where(AuditLog.action.in_(PUBLIC_ACTIONS))
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "id": str(log.id),
            "action": log.action,
            "username": username or "system",
            "target_type": log.target_type,
            "target_id": log.target_id,
            "created_at": log.created_at.isoformat(),
        }
        for log, username in rows
    ]
