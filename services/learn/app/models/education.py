"""강의·과제 교육 운영 도메인 모델.

기존 ``Course``/``Lesson``은 공개 학습 콘텐츠이고, 이 모듈의 ``TeachingClass``와
``EducationAssignment``는 교수·수강생 단위의 교육 운영 기록이다. 두 도메인을
연결할 때에는 ``EducationResourceLink``의 읽기 전용 참조만 사용하며, 코스 진척도,
지식 그래프, 워게임/CTF 점수에는 어떤 쓰기 부작용도 만들지 않는다.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.models.course import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EducationRole(str, enum.Enum):
    """Learn DB 안에서만 적용되는 추가 교육 역할."""

    PROFESSOR_ADMIN = "professor_admin"


class TeachingClassStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EnrollmentStatus(str, enum.Enum):
    ACTIVE = "active"
    DROPPED = "dropped"


class AssignmentStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EducationResourceType(str, enum.Enum):
    COURSE = "course"
    LESSON = "lesson"
    WARGAME = "wargame"


class EducationAssetScope(str, enum.Enum):
    CLASS = "class"
    ASSIGNMENT = "assignment"


class GradeStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class EducationRoleGrant(Base):
    """전역 Auth 역할을 바꾸지 않는 교육 도메인 전용 권한 부여 기록.

    Auth 서비스가 사용자·밴·전역 역할의 기준 데이터다. 이 테이블은 해당 사용자가
    ``professor_admin`` 권한을 받은 이력만 보관하며, 활성 권한은 한 사용자당 하나다.
    """

    __tablename__ = "education_role_grants"
    __table_args__ = (
        CheckConstraint(
            "role = 'professor_admin'",
            name="ck_education_role_grants_role",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_education_role_grants_revoke_after_grant",
        ),
        Index(
            "uq_education_role_grants_active_subject",
            "subject_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_education_role_grants_subject_active",
            "subject_id",
            "revoked_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Auth DB가 원본을 소유하므로 교차 서비스 FK는 만들지 않는다.
    subject_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    role = Column(String(32), nullable=False, default=EducationRole.PROFESSOR_ADMIN.value)
    granted_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    grant_reason = Column(Text, nullable=False)
    granted_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, index=True)
    revoked_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    revoke_reason = Column(Text, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)


class TeachingClass(Base):
    """코스와 구분되는 교수 운영 강의."""

    __tablename__ = "teaching_classes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_teaching_classes_status",
        ),
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_teaching_classes_schedule",
        ),
        Index("ix_teaching_classes_status_starts_at", "status", "starts_at"),
        Index("ix_teaching_classes_instructor_status", "instructor_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    summary = Column(String(500), nullable=True)
    description_markdown = Column(Text, nullable=False, default="")
    instructor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=TeachingClassStatus.DRAFT.value, index=True)
    starts_at = Column(DateTime(timezone=True), nullable=True, index=True)
    ends_at = Column(DateTime(timezone=True), nullable=True, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archived_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    archive_reason = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    updated_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class TeachingClassEnrollment(Base):
    """수강생 등록 이력. 탈퇴 행도 보존해 재등록 시 중복 행을 만들지 않는다."""

    __tablename__ = "teaching_class_enrollments"
    __table_args__ = (
        UniqueConstraint("class_id", "student_id", name="uq_teaching_class_enrollments_student"),
        CheckConstraint(
            "status IN ('active','dropped')",
            name="ck_teaching_class_enrollments_status",
        ),
        CheckConstraint(
            "(status = 'active' AND dropped_at IS NULL) OR "
            "(status = 'dropped' AND dropped_at IS NOT NULL)",
            name="ck_teaching_class_enrollments_drop_state",
        ),
        Index("ix_teaching_class_enrollments_student_status", "student_id", "status"),
        Index("ix_teaching_class_enrollments_class_status", "class_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_id = Column(
        UUID(as_uuid=True),
        ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    student_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=EnrollmentStatus.ACTIVE.value, index=True)
    enrolled_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    enrolled_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    dropped_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    dropped_at = Column(DateTime(timezone=True), nullable=True)


class EducationAssignment(Base):
    """강의에 귀속되는 텍스트 제출형 과제."""

    __tablename__ = "education_assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_education_assignments_status",
        ),
        CheckConstraint("max_score BETWEEN 1 AND 1000", name="ck_education_assignments_max_score"),
        CheckConstraint(
            "due_at IS NULL OR available_from_at IS NULL OR due_at > available_from_at",
            name="ck_education_assignments_submission_window",
        ),
        Index("ix_education_assignments_class_status", "class_id", "status"),
        Index("ix_education_assignments_class_due_at", "class_id", "due_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_id = Column(
        UUID(as_uuid=True),
        ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    title = Column(String(200), nullable=False)
    instructions_markdown = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default=AssignmentStatus.DRAFT.value, index=True)
    max_score = Column(Integer, nullable=False, default=100)
    available_from_at = Column(DateTime(timezone=True), nullable=True, index=True)
    due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archived_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    archive_reason = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    updated_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class EducationResourceLink(Base):
    """공개 Course/Lesson/워게임만 강의에 읽기 전용으로 연결한다."""

    __tablename__ = "education_resource_links"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('course','lesson','wargame')",
            name="ck_education_resource_links_type",
        ),
        CheckConstraint(
            "(resource_type = 'course' AND course_id IS NOT NULL AND lesson_id IS NULL AND challenge_id IS NULL) OR "
            "(resource_type = 'lesson' AND course_id IS NULL AND lesson_id IS NOT NULL AND challenge_id IS NULL) OR "
            "(resource_type = 'wargame' AND course_id IS NULL AND lesson_id IS NULL AND challenge_id IS NOT NULL)",
            name="ck_education_resource_links_exact_target",
        ),
        Index(
            "uq_education_resource_links_course",
            "class_id",
            "course_id",
            unique=True,
            postgresql_where=text("resource_type = 'course'"),
        ),
        Index(
            "uq_education_resource_links_lesson",
            "class_id",
            "lesson_id",
            unique=True,
            postgresql_where=text("resource_type = 'lesson'"),
        ),
        Index(
            "uq_education_resource_links_wargame",
            "class_id",
            "challenge_id",
            unique=True,
            postgresql_where=text("resource_type = 'wargame'"),
        ),
        Index("ix_education_resource_links_class_created", "class_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_id = Column(
        UUID(as_uuid=True),
        ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    resource_type = Column(String(20), nullable=False, index=True)
    course_id = Column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    lesson_id = Column(
        UUID(as_uuid=True),
        ForeignKey("lessons.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    challenge_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    label = Column(String(200), nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)


class AssignmentSubmission(Base):
    """수강생의 텍스트 Markdown 과제 제출.

    본문은 채점에 필요한 원본으로만 이 테이블에 둔다. ``EducationAuditEvent``에는
    본문과 피드백 원문을 복사하지 않는다.
    """

    __tablename__ = "education_assignment_submissions"
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", name="uq_education_assignment_submissions_student"),
        Index("ix_education_assignment_submissions_assignment_submitted", "assignment_id", "submitted_at"),
        Index("ix_education_assignment_submissions_student_updated", "student_id", "updated_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assignment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("education_assignments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    student_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    content_markdown = Column(Text, nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class AssignmentGrade(Base):
    """제출과 1:1로 연결되는 성적. 공개 전 draft는 수강생에게 숨긴다."""

    __tablename__ = "education_assignment_grades"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_education_assignment_grades_submission"),
        CheckConstraint("score >= 0", name="ck_education_assignment_grades_score_nonnegative"),
        CheckConstraint(
            "status IN ('draft','published')",
            name="ck_education_assignment_grades_status",
        ),
        Index("ix_education_assignment_grades_status_published", "status", "published_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id = Column(
        UUID(as_uuid=True),
        ForeignKey("education_assignment_submissions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    score = Column(Integer, nullable=False)
    feedback_markdown = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=GradeStatus.DRAFT.value, index=True)
    graded_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    graded_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    published_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class EducationAsset(Base):
    """교수 자료의 private object metadata.

    ``object_path``는 서버 내부 객체 경로이며 API 응답에 포함하지 않는다. 과제 자료도
    항상 ``class_id``를 함께 가져 수업 단위 quota를 원자적으로 계산할 수 있게 한다.
    학생 제출은 V1에서 텍스트 Markdown만 허용하므로 이 테이블을 사용하지 않는다.
    """

    __tablename__ = "education_assets"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('class','assignment')",
            name="ck_education_assets_scope",
        ),
        CheckConstraint(
            "(scope = 'class' AND assignment_id IS NULL) OR "
            "(scope = 'assignment' AND assignment_id IS NOT NULL)",
            name="ck_education_assets_scope_target",
        ),
        CheckConstraint("byte_size > 0", name="ck_education_assets_positive_size"),
        CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="ck_education_assets_delete_after_create",
        ),
        Index("ix_education_assets_class_active", "class_id", "deleted_at"),
        Index("ix_education_assets_assignment_active", "assignment_id", "deleted_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_id = Column(
        UUID(as_uuid=True),
        ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assignment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("education_assignments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    scope = Column(String(20), nullable=False, index=True)
    # 절대 응답·감사 JSON에 복제하지 않는다. 업로드 helper가 생성한 private prefix만 허용.
    object_path = Column(String(1024), nullable=False, unique=True)
    stored_name = Column(String(180), nullable=False)
    original_name = Column(String(255), nullable=False)
    content_type = Column(String(255), nullable=False)
    byte_size = Column(Integer, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    deleted_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    delete_reason = Column(Text, nullable=True)


class EducationAssetCleanupJob(Base):
    """Private education object 삭제를 위한 durable outbox/lease 원장."""

    __tablename__ = "education_asset_cleanup_jobs"
    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_education_asset_cleanup_jobs_asset"),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_education_asset_cleanup_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_education_asset_cleanup_jobs_attempt_nonnegative",
        ),
        Index("ix_education_asset_cleanup_jobs_claim", "status", "available_at"),
        Index("ix_education_asset_cleanup_jobs_lease", "status", "lease_until"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id = Column(
        UUID(as_uuid=True),
        ForeignKey("education_assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    object_path = Column(String(1024), nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    available_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, index=True)
    lease_until = Column(DateTime(timezone=True), nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class EducationAuditEvent(Base):
    """교육 관리 변경의 append-only 최소 감사 원장.

    ``details``에는 상태, 대상 ID, 길이, 공개 여부처럼 감사에 필요한 메타데이터만
    기록한다. 수강생 제출 본문, 성적 피드백, Auth 토큰, 요청 헤더는 절대 넣지 않는다.
    """

    __tablename__ = "education_audit_events"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('role_grant','class','enrollment','assignment','resource_link','submission','grade','asset')",
            name="ck_education_audit_events_entity_type",
        ),
        Index("ix_education_audit_events_class_created", "class_id", "created_at"),
        Index("ix_education_audit_events_actor_created", "actor_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False, index=True)
    entity_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    class_id = Column(
        UUID(as_uuid=True),
        ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    details = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, index=True)
