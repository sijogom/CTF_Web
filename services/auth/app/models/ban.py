"""계정 밴과 이의제기 원장 모델.

`User.is_active`는 탈퇴·운영상 비활성화와 호환성을 유지한다. 징계성 접근 차단은
이 원장에서 계산하며, 밴과 해제 기록을 삭제하지 않는다.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID

from app.models.user import Base


class AccountBan(Base):
    __tablename__ = "account_bans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 사용자 삭제 이후에도 감사 식별자를 보존하기 위해 FK는 두지 않는다.
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    public_reason = Column(Text, nullable=False)
    evidence_summary = Column(Text, nullable=False)
    policy_version = Column(String(100), nullable=False)
    source = Column(String(20), nullable=False, default="manual")
    detection_rule_version = Column(String(100), nullable=True)
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    lifted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    lifted_by = Column(UUID(as_uuid=True), nullable=True)
    lift_reason = Column(Text, nullable=True)
    appeal_allowed = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("source IN ('manual', 'automated')", name="ck_account_bans_source"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > started_at",
            name="ck_account_bans_expiry_after_start",
        ),
        Index("ix_account_bans_user_started", "user_id", "started_at"),
        Index(
            "ix_account_bans_active_lookup",
            "user_id",
            "started_at",
            "expires_at",
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )


class BanAppeal(Base):
    __tablename__ = "ban_appeals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ban_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    message = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    reviewer_id = Column(UUID(as_uuid=True), nullable=True)
    reviewer_note = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_ban_appeals_status",
        ),
        Index(
            "uq_ban_appeals_one_pending_per_ban",
            "ban_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
