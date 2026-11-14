"""플랫폼 이벤트 원본과 변경 감사 모델.

이벤트는 CTF 대회와 커뮤니티 공지와 다른 Learn 콘텐츠 도메인이다.  따라서 이
모듈은 Competition 또는 Community 모델을 참조하지 않는다.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.models.course import Base


class EventStatus(str, enum.Enum):
    """플랫폼 이벤트의 공개·보관 상태."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PlatformEventCategory(Base):
    __tablename__ = "platform_event_categories"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_platform_event_categories_revision"),
        CheckConstraint("sort_order >= 0 AND sort_order <= 1000000", name="ck_platform_event_categories_sort_order"),
        Index("ix_platform_event_categories_order", "sort_order", "key"),
        Index("uq_platform_event_categories_ko_label", text("lower(labels->>'ko')"), unique=True),
        UniqueConstraint("operation_id", name="uq_platform_event_categories_operation"),
    )

    key = Column(String(30), primary_key=True)
    labels = Column(JSONB, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=1)
    operation_id = Column(UUID(as_uuid=True), nullable=True)
    create_request_hash = Column(String(64), nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    updated_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class PlatformEventCategoryAuditEvent(Base):
    __tablename__ = "platform_event_category_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_key = Column(String(30), ForeignKey("platform_event_categories.key", ondelete="RESTRICT"), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(String(64), nullable=False)
    details = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class PlatformEvent(Base):
    """대회·공지와 분리된 플랫폼 이벤트."""

    __tablename__ = "platform_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_platform_events_status",
        ),
        CheckConstraint("ends_at > starts_at", name="ck_platform_events_schedule"),
        CheckConstraint(
            "visible_until_at IS NULL OR visible_until_at > visible_from_at",
            name="ck_platform_events_visibility_window",
        ),
        Index("ix_platform_events_status_visible_from", "status", "visible_from_at"),
        Index("ix_platform_events_category_starts_at", "category", "starts_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    summary = Column(String(500), nullable=True)
    content_markdown = Column(Text, nullable=False)
    category = Column(String(30), ForeignKey("platform_event_categories.key", ondelete="RESTRICT"), nullable=False, index=True)
    category_definition = relationship(PlatformEventCategory, lazy="selectin")
    location = Column(String(300), nullable=True)
    external_url = Column(String(2048), nullable=True)

    starts_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ends_at = Column(DateTime(timezone=True), nullable=False, index=True)
    visible_from_at = Column(DateTime(timezone=True), nullable=False, index=True)
    visible_until_at = Column(DateTime(timezone=True), nullable=True, index=True)

    status = Column(String(20), nullable=False, default=EventStatus.DRAFT.value, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archive_reason = Column(Text, nullable=True)

    # Auth service가 계정 원본을 소유하므로 서비스 간 FK는 만들지 않는다.
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    updated_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    archived_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PlatformEventAuditEvent(Base):
    """이벤트 변경의 append-only 감사 행.

    원문 본문이나 요청 인증정보는 복제하지 않고 상태·분류·기간 등의 최소 메타데이터만
    ``details``에 저장한다.
    """

    __tablename__ = "platform_event_audit_events"
    __table_args__ = (
        Index("ix_platform_event_audit_events_event_created", "event_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_events.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    details = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
