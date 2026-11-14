import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ctfd_user_id = Column(Integer, unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)
    oauth_provider = Column(String(20), nullable=True)
    oauth_id = Column(String(255), nullable=True)
    role = Column(String(20), default="user", nullable=False)
    level = Column(Integer, default=1, nullable=False)
    xp = Column(Integer, default=0, nullable=False)
    points = Column(Integer, default=0, nullable=False)
    streak_days = Column(Integer, default=0, nullable=False)
    avatar_url = Column(String(500), nullable=True)
    bio = Column(Text, nullable=True)
    # 2FA (TOTP)
    totp_secret = Column(String(32), nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False)
    backup_codes = Column(ARRAY(Text), nullable=True)
    # Discord 연동
    discord_id = Column(String(50), unique=True, nullable=True, index=True)
    discord_username = Column(String(100), nullable=True)
    # 백준(solved.ac) 연동
    baekjoon_handle = Column(String(50), unique=True, nullable=True, index=True)
    baekjoon_tier = Column(Integer, nullable=True)  # solved.ac 티어 번호 (0~30)
    # 티어 강제 설정 (NULL이면 XP 기반 자동, 값이 있으면 해당 티어명으로 고정)
    tier_override = Column(String(50), nullable=True)
    # 계정 상태
    is_active = Column(Boolean, default=True, nullable=False)
    # 이메일 인증
    email_verified = Column(Boolean, default=False, nullable=False)
    email_verify_token = Column(String(255), nullable=True, index=True)
    email_verify_expires = Column(DateTime(timezone=True), nullable=True)
    # 신규 가입 코드 인증. 기존 링크 인증 계정은 code_required=False로 호환한다.
    email_verify_code_required = Column(Boolean, default=False, nullable=False)
    email_verify_code_hash = Column(String(128), nullable=True)
    email_verify_code_expires = Column(DateTime(timezone=True), nullable=True)
    email_verify_code_sent_at = Column(DateTime(timezone=True), nullable=True)
    email_verify_code_attempts = Column(Integer, default=0, nullable=False)
    # 이메일 수신 설정 (구독 관리)
    email_notifications = Column(Boolean, default=True, nullable=False)
    comment_notifications = Column(Boolean, default=True, server_default="true", nullable=False)
    competition_notifications = Column(Boolean, default=True, server_default="true", nullable=False)
    badge_notifications = Column(Boolean, default=True, server_default="true", nullable=False)
    # 타임스탬프
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class EmailLog(Base):
    __tablename__ = "email_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sender_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    recipients = Column(Text, nullable=False)  # JSON 배열 (마스킹된 이메일)
    subject = Column(String(500), nullable=False)
    target_type = Column(String(20), nullable=False)  # "user", "role", "all", "system"
    total_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    fail_count = Column(Integer, default=0, nullable=False)
    error_details = Column(Text, nullable=True)  # JSON (마스킹된 이메일: 에러 메시지)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
