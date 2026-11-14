"""모더레이션 확인 토큰과 변경 불가능한 감사 이벤트 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    event as sqlalchemy_event,
)
from sqlalchemy.dialects.postgresql import UUID

from app.models.post import Base


class ModerationConfirmation(Base):
    """한 번만 사용할 수 있는 서버 측 모더레이션 확인 토큰의 해시."""

    __tablename__ = "moderation_confirmations"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('post', 'comment', 'marketplace_item', 'marketplace_comment')",
            name="ck_moderation_confirmation_target_type",
        ),
        CheckConstraint(
            "action IN ('hide', 'restore')",
            name="ck_moderation_confirmation_action",
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND consumed_by IS NULL) "
            "OR (consumed_at IS NOT NULL AND consumed_by IS NOT NULL)",
            name="ck_moderation_confirmation_consumed_pair",
        ),
        Index(
            "ix_moderation_confirmation_requester_expiry",
            "requested_by",
            "expires_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_digest = Column(String(64), nullable=False, unique=True, index=True)
    target_type = Column(String(20), nullable=False, index=True)
    target_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_name = Column(String(300), nullable=False)
    target_fingerprint = Column(String(64), nullable=False)
    action = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False)
    requested_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    issued_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    consumed_by = Column(UUID(as_uuid=True), nullable=True)


class ModerationEvent(Base):
    """숨김/복구 실행의 append-only 감사 이벤트."""

    __tablename__ = "moderation_events"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('post', 'comment', 'marketplace_item', 'marketplace_comment')",
            name="ck_moderation_event_target_type",
        ),
        CheckConstraint(
            "action IN ('hide', 'restore')",
            name="ck_moderation_event_action",
        ),
        CheckConstraint(
            "prior_hidden IS DISTINCT FROM resulting_hidden",
            name="ck_moderation_event_state_change",
        ),
        Index("ix_moderation_event_target_created", "target_type", "target_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("moderation_confirmations.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    target_type = Column(String(20), nullable=False, index=True)
    target_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_name = Column(String(300), nullable=False)
    impact_scope = Column(String(100), nullable=False)
    action = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    actor_role = Column(String(20), nullable=False)
    prior_hidden = Column(Boolean, nullable=False)
    resulting_hidden = Column(Boolean, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )


@sqlalchemy_event.listens_for(ModerationEvent, "before_update")
def _prevent_moderation_event_update(_mapper, _connection, _target) -> None:
    """애플리케이션 ORM 경로에서 감사 이벤트 변경을 차단한다."""
    raise ValueError("Moderation events are immutable.")


@sqlalchemy_event.listens_for(ModerationEvent, "before_delete")
def _prevent_moderation_event_delete(_mapper, _connection, _target) -> None:
    """애플리케이션 ORM 경로에서 감사 이벤트 삭제를 차단한다."""
    raise ValueError("Moderation events are immutable.")
