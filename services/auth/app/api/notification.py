"""알림 API — 조회 + 관리자 생성."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, literal, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.notification import Notification
from app.models.personal_notification import PersonalNotification
from app.api.deps import get_current_user, require_role
from app.utils.safe_html import sanitize_rich_content

router = APIRouter(tags=["알림"])


# ──── 스키마 ────

class NotificationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = ""
    type: str = Field("toast", pattern=r"^(toast|popup|alert)$")

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="알림 내용",
            allow_empty=True,
            max_visible_chars=5000,
        )


class NotificationResponse(BaseModel):
    id: str
    title: str
    content: str
    type: str
    created_at: str


# ──── 알림 목록 조회 ────

@router.get("/notifications")
async def list_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """알림 목록 조회 (최신순)."""
    return await notification_page(db, user.id, page, size)


async def notification_page(db, user_id, page, size, *, global_only=False):
    global_query = select(Notification.id, Notification.title, Notification.content,
        Notification.type, Notification.created_at, literal("global").label("source"),
        literal(None).label("path"))
    personal_query = select(PersonalNotification.id, PersonalNotification.title,
        literal("").label("content"), literal("toast").label("type"), PersonalNotification.created_at,
        literal("personal").label("source"), PersonalNotification.path).where(
            PersonalNotification.user_id == user_id, PersonalNotification.visible.is_(True))
    combined = (global_query if global_only else union_all(global_query, personal_query)).subquery()
    total = (await db.execute(select(func.count()).select_from(combined))).scalar_one()
    rows = (await db.execute(select(combined).order_by(combined.c.created_at.desc(), combined.c.id.desc())
        .offset((page - 1) * size).limit(size))).mappings().all()
    return {"items": [{**dict(row), "id": str(row["id"]),
                       "created_at": row["created_at"].isoformat()} for row in rows],
            "total": total, "page": page, "size": size}


@router.get("/admin/notifications")
async def list_admin_notifications(
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_role("admin", "moderator")),
):
    return await notification_page(db, user.id, page, size, global_only=True)


# ──── 관리자: 알림 생성 ────

@router.post("/admin/notifications", status_code=201)
async def create_notification(
    body: NotificationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """알림 생성 (관리자/모더레이터 전용)."""
    notification = Notification(
        title=body.title,
        content=body.content,
        type=body.type,
        created_by=user.id,
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)

    return {
        "id": str(notification.id),
        "title": notification.title,
        "type": notification.type,
        "message": "알림이 생성되었습니다.",
    }


# ──── 관리자: 알림 삭제 ────

@router.delete("/admin/notifications/{notification_id}")
async def delete_notification(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """알림 삭제 (관리자 전용)."""
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )
    notification = result.scalar_one_or_none()
    if not notification:
        raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")

    await db.delete(notification)
    await db.commit()
    return {"message": "알림이 삭제되었습니다."}
