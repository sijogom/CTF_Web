"""Community outbox를 위한 내부 사용자별 멱등 알림 수신."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal import verify_internal_key
from app.database import get_db
from app.models.user import User
from app.utils.personal_notifications import enqueue_personal_notification

router = APIRouter(tags=["internal-notifications"], dependencies=[Depends(verify_internal_key)])


class CommentNotificationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    recipient_id: UUID
    post_id: UUID
    kind: Literal["comment"] = "comment"


@router.post("/personal-notifications")
async def receive_comment_notification(body: CommentNotificationEvent, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, body.recipient_id)
    if user is None:
        return {"status": "skipped"}
    created = await enqueue_personal_notification(db, user=user,
        event_id=f"comment:{body.event_id}", kind="comment",
        title="내 게시글에 새 댓글이 등록되었습니다.", path=f"/community/{body.post_id}")
    await db.commit()
    return {"status": "accepted", "created": created}
