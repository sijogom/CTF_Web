"""감사 로그 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.models.user import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    target_type = Column(String(50), nullable=True)
    target_id = Column(String(255), nullable=True)
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # 복합 인덱스: 최근 감사 로그 조회 최적화
    __table_args__ = (
        Index("idx_audit_logs_created_at_actor_id", "created_at", "actor_id"),
        Index("idx_audit_logs_created_at_action", "created_at", "action"),
    )
