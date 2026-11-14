"""알림(Notification) DB 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Text, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.user import Base


class Notification(Base):
    """플랫폼 전체 알림 테이블."""
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "type IN ('toast','popup','alert')",
            name="ck_notifications_type",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False, default="")
    type = Column(String(20), nullable=False, default="toast")  # toast, popup, alert
    created_by = Column(UUID(as_uuid=True), nullable=True)  # 작성자 user_id
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
