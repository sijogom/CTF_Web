import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, String, Integer, Boolean, DateTime, Text,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ContentStatus(str, enum.Enum):
    """콘텐츠 승인 상태."""
    DRAFT = "draft"          # 작성 중 (작성자만 볼 수 있음)
    PENDING = "pending"      # 승인 대기 (관리자 검토 필요)
    APPROVED = "approved"    # 승인됨 (일반 사용자 열람 가능)
    REJECTED = "rejected"    # 거부됨 (사유와 함께 작성자에게 통보)


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint(
            "category IN ('web','crypto','pwn','rev','forensic','misc')",
            name="ck_courses_category",
        ),
        # 운영 DB 전환은 Learn Alembic 20260813_002 revision이 수행한다.
        # 기존 데이터 매핑: beginner→introductory, easy→basic,
        # medium_low/medium/medium_high→intermediate, hard/expert/impossible→advanced.
        CheckConstraint(
            "difficulty IN ('introductory','basic','intermediate','advanced')",
            name="ck_courses_difficulty",
        ),
        CheckConstraint(
            "estimated_hours > 0",
            name="ck_courses_estimated_hours_positive",
        ),
        CheckConstraint(
            "status IN ('draft','pending','approved','rejected')",
            name="ck_courses_status",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=False, index=True)       # web, pwn, rev, crypto, forensic, misc
    difficulty = Column(String(20), nullable=False, index=True)     # beginner, intermediate, advanced
    estimated_hours = Column(Integer, default=1, nullable=False)
    thumbnail_url = Column(String(500), nullable=True)
    author_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # platform_db.users FK 없음 (크로스 DB)
    is_published = Column(Boolean, default=False, nullable=False)
    order_index = Column(Integer, default=0, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    deletion_reason = Column(Text, nullable=True)
    published_before_deletion = Column(Boolean, default=False, nullable=False)

    # 콘텐츠 승인 상태
    status = Column(
        String(20),
        default=ContentStatus.DRAFT.value,
        nullable=False,
        index=True,
    )
    rejection_reason = Column(Text, nullable=True)           # 거부 사유
    reviewed_by = Column(UUID(as_uuid=True), nullable=True)  # 승인/거부한 관리자 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    lessons = relationship("Lesson", back_populates="course", order_by="Lesson.order_index")
    prerequisites = relationship("CoursePrerequisite", foreign_keys="CoursePrerequisite.course_id")


class Lesson(Base):
    __tablename__ = "lessons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)          # 마크다운 본문
    order_index = Column(Integer, default=0, nullable=False)
    estimated_minutes = Column(Integer, default=10, nullable=False)
    # 연관 챌린지 (CTFd challenge ID — 실습 연동)
    challenge_id = Column(Integer, nullable=True)
    # 첨부파일 메타데이터 (JSONB 배열)
    attachments = Column(JSONB, default=list, nullable=False, server_default="[]")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    course = relationship("Course", back_populates="lessons")


class CoursePrerequisite(Base):
    __tablename__ = "course_prerequisites"
    __table_args__ = (
        UniqueConstraint("course_id", "prerequisite_id", name="uq_course_prereq"),
        CheckConstraint(
            "course_id != prerequisite_id",
            name="ck_course_prereq_no_self_ref",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    prerequisite_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)


class CourseDeletionConfirmation(Base):
    """코스 보관 삭제를 위한 단기·1회성 서버 확인 토큰."""
    __tablename__ = "course_deletion_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    reason = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CourseAuditEvent(Base):
    """코스 삭제·복구의 최소 감사 이벤트. 본문·첨부 원문은 복제하지 않는다."""
    __tablename__ = "course_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    details = Column(JSONB, default=dict, nullable=False, server_default="{}")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class UserProgress(Base):
    __tablename__ = "user_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)   # platform_db.users FK 없음 (크로스 DB)
    lesson_id = Column(UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True)
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
