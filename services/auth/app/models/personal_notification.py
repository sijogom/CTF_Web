"""개인 알림 receipt, 전달 outbox와 최초 이벤트 기준선."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.user import Base


def utcnow():
    return datetime.now(timezone.utc)


class PersonalNotification(Base):
    __tablename__ = "personal_notifications"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(String(180), nullable=False)
    payload_digest = Column(String(64), nullable=False)
    kind = Column(String(20), nullable=False)
    title = Column(String(200), nullable=False)
    path = Column(String(300), nullable=False)
    visible = Column(Boolean, nullable=False, default=True)
    email_status = Column(String(20), nullable=False, default="skipped")
    email_attempt_count = Column(Integer, nullable=False, default=0)
    email_next_attempt_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    email_started_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_personal_notifications_user_event"),
        CheckConstraint("kind IN ('comment','competition','badge')", name="ck_personal_notifications_kind"),
        CheckConstraint("email_status IN ('pending','sending','sent','skipped','uncertain')", name="ck_personal_notifications_email_status"),
        Index("ix_personal_notifications_user_created", "user_id", "created_at"),
        Index("ix_personal_notifications_email_pending", "email_status", "email_next_attempt_at"),
    )


class NotificationPolicy(Base):
    __tablename__ = "notification_policies"
    id = Column(String(30), primary_key=True)
    activated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    baseline_completed_at = Column(DateTime(timezone=True), nullable=True)


class NotificationEvent(Base):
    __tablename__ = "personal_notification_events"
    event_id = Column(String(180), primary_key=True)
    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(20), nullable=False, default="competition")
    title = Column(String(200), nullable=False)
    path = Column(String(300), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_user_id = Column(UUID(as_uuid=True), nullable=True)
    complete = Column(Boolean, nullable=False, default=False)


class CompetitionNotificationState(Base):
    __tablename__ = "competition_notification_states"
    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    published_recorded = Column(Boolean, nullable=False, default=False)
    started_recorded = Column(Boolean, nullable=False, default=False)


class BadgeObservation(Base):
    __tablename__ = "badge_observations"
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    badge_id = Column(String(80), primary_key=True)
    earned = Column(Boolean, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    earned_at = Column(DateTime(timezone=True), nullable=True)
