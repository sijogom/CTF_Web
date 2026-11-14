"""레슨 본문 이미지의 staged/attached/orphan 정리 원장."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.course import Base


class LessonImageAsset(Base):
    """본문에서 실제로 참조되는지 재조정할 수 있는 이미지 메타데이터."""

    __tablename__ = "lesson_image_assets"
    __table_args__ = (
        UniqueConstraint("object_path", name="uq_lesson_image_assets_object_path"),
        UniqueConstraint("folder_id", "filename", name="uq_lesson_image_assets_location"),
        CheckConstraint(
            "state IN ('staged','attached','orphan_candidate','cleanup_pending','deleted')",
            name="ck_lesson_image_assets_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_lesson_image_assets_attempt_nonnegative"),
        Index("ix_lesson_image_assets_state_cleanup", "state", "cleanup_after"),
        Index("ix_lesson_image_assets_lease", "state", "lease_until"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_path = Column(String(1024), nullable=False)
    folder_id = Column(String(12), nullable=False)
    filename = Column(String(64), nullable=False)
    original_name = Column(String(255), nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    state = Column(String(24), nullable=False, default="staged", server_default="staged")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    cleanup_after = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(String(64), nullable=True)
