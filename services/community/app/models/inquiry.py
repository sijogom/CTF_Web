"""1:1 문의와 안전한 비회원 조회·알림 outbox 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, JSON, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.post import Base


class Inquiry(Base):
    """1:1 문의 원본."""

    __tablename__ = "inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    email = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    locale = Column(String(10), default="ko", nullable=False)
    subject = Column(String(300), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(20), default="open", nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    replies = relationship(
        "InquiryReply",
        back_populates="inquiry",
        order_by="InquiryReply.created_at",
    )
    events = relationship(
        "InquiryEvent",
        back_populates="inquiry",
        order_by="InquiryEvent.created_at",
        cascade="all, delete-orphan",
    )
    access_challenges = relationship(
        "InquiryAccessChallenge",
        back_populates="inquiry",
        order_by="InquiryAccessChallenge.created_at",
        cascade="all, delete-orphan",
    )
    access_sessions = relationship(
        "InquiryAccessSession",
        back_populates="inquiry",
        order_by="InquiryAccessSession.created_at",
        cascade="all, delete-orphan",
    )
    notification_outbox = relationship(
        "InquiryNotificationOutbox",
        back_populates="inquiry",
        order_by="InquiryNotificationOutbox.created_at",
        cascade="all, delete-orphan",
    )


class InquiryReply(Base):
    """관리자 또는 모더레이터 답변."""

    __tablename__ = "inquiry_replies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inquiry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    admin_id = Column(UUID(as_uuid=True), nullable=False)
    admin_name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    inquiry = relationship("Inquiry", back_populates="replies")


class InquiryEvent(Base):
    """문의 처리의 application append-only 감사 이벤트."""

    __tablename__ = "inquiry_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inquiry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_type = Column(String(20), nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    action = Column(String(80), nullable=False, index=True)
    details = Column(JSON, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    inquiry = relationship("Inquiry", back_populates="events")


class InquiryAccessChallenge(Base):
    """비회원 이메일 소유 증명. raw proof는 DB에 저장하지 않는다."""

    __tablename__ = "inquiry_access_challenges"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('guest_access', 'reply_access')",
            name="ck_inquiry_access_challenges_purpose",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_inquiry_access_challenges_expiry",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inquiry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose = Column(String(20), nullable=False, default="guest_access")
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    inquiry = relationship("Inquiry", back_populates="access_challenges")
    outbox_items = relationship("InquiryNotificationOutbox", back_populates="access_challenge")


class InquiryAccessSession(Base):
    """짧은 read-only 비회원 세션. raw token은 cookie에만 존재한다."""

    __tablename__ = "inquiry_access_sessions"
    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at",
            name="ck_inquiry_access_sessions_expiry",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inquiry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_digest = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    inquiry = relationship("Inquiry", back_populates="access_sessions")


class InquiryNotificationOutbox(Base):
    """Community가 Auth에 전달할 durable 이메일 명령. 본문은 저장하지 않는다."""

    __tablename__ = "inquiry_notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "recipient_type IN ('guest', 'member')",
            name="ck_inquiry_notification_outbox_recipient_type",
        ),
        CheckConstraint(
            "kind IN ('guest_access', 'reply_available')",
            name="ck_inquiry_notification_outbox_kind",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'sent', 'skipped')",
            name="ck_inquiry_notification_outbox_state",
        ),
        CheckConstraint(
            "(recipient_type = 'guest' AND recipient_user_id IS NULL) "
            "OR (recipient_type = 'member' AND recipient_user_id IS NOT NULL)",
            name="ck_inquiry_notification_outbox_recipient_ref",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inquiry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reply_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiry_replies.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    challenge_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inquiry_access_challenges.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recipient_type = Column(String(20), nullable=False)
    recipient_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    kind = Column(String(30), nullable=False)
    locale = Column(String(10), nullable=False, default="ko")
    dedupe_key = Column(String(160), nullable=False, unique=True)
    state = Column(String(20), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    available_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    lease_token_digest = Column(String(64), nullable=True)
    lease_owner = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    inquiry = relationship("Inquiry", back_populates="notification_outbox")
    access_challenge = relationship("InquiryAccessChallenge", back_populates="outbox_items")
