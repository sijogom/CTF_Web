"""삭제를 통한 객체 손실 없이 staged 업로드와 문제 연결을 분리한다."""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.course import Base


class ChallengeAttachmentDraft(Base):
    __tablename__ = "challenge_attachment_drafts"
    __table_args__ = (
        CheckConstraint("state IN ('open','bound')", name="ck_challenge_attachment_draft_state"),
        CheckConstraint("base_attachment_revision >= 0", name="ck_challenge_attachment_draft_revision"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True)
    owner_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    # 원래 대상 ID는 삭제 후에도 보존하여 다른 문제에 재사용하지 않는다.
    challenge_id = Column(Integer, nullable=True)
    original_challenge_id = Column(Integer, nullable=True)
    base_attachment_revision = Column(Integer, nullable=False, default=0, server_default="0")
    state = Column(String(16), nullable=False, default="open", server_default="open")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class ChallengeAttachment(Base):
    __tablename__ = "challenge_attachments"
    __table_args__ = (
        CheckConstraint("byte_size >= 0 AND byte_size <= 104857600", name="ck_challenge_attachment_size"),
        CheckConstraint("state IN ('pending','ready')", name="ck_challenge_attachment_state"),
        Index("ix_challenge_attachments_parent_position", "challenge_id", "position"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True)
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    draft_id = Column(UUID(as_uuid=True), ForeignKey("challenge_attachment_drafts.id"), nullable=False, index=True)
    original_name = Column(Text, nullable=False)
    byte_size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    content_type = Column(String(128), nullable=False)
    object_path = Column(Text, nullable=False, unique=True)
    state = Column(String(16), nullable=False, default="pending", server_default="pending")
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True)
    bound_challenge_id = Column(Integer, nullable=True)
    position = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
