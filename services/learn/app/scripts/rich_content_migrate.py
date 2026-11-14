"""Inventory, apply, or roll back Learn canonical HTML conversion."""

from app.config import settings
from app.database import engine
from app.models.challenge import Challenge, Hint
from app.models.challenge_submission import ChallengeSubmission
from app.models.course import Course, Lesson
from app.models.education import (
    AssignmentGrade,
    AssignmentSubmission,
    EducationAssignment,
    TeachingClass,
)
from app.models.event import PlatformEvent
from app.utils.rich_content_migration import RichContentFieldSpec, run_cli


SPECS = (
    RichContentFieldSpec(
        Course.__table__,
        "description",
        field_name="코스 설명",
        allow_empty=True,
        nullable=True,
        guard_columns=("author_id", "status", "is_published", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Lesson.__table__,
        "content",
        field_name="레슨 본문",
        allow_empty=True,
        nullable=True,
        guard_columns=("course_id", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Challenge.__table__,
        "description",
        field_name="문제 설명",
        allow_empty=True,
        max_visible_chars=10000,
        guard_columns=("author_id", "is_published", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Challenge.__table__,
        "description_i18n",
        kind="mapping",
        field_name="문제 설명 번역",
        allow_empty=True,
        max_visible_chars=10000,
        guard_columns=("author_id", "is_published", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Hint.__table__,
        "content",
        field_name="힌트",
        max_visible_chars=2000,
        guard_columns=("challenge_id", "cost", "order_index"),
    ),
    RichContentFieldSpec(
        ChallengeSubmission.__table__,
        "description",
        field_name="제출 문제 설명",
        allow_empty=True,
        max_visible_chars=10000,
        guard_columns=("submitter_id", "status", "reviewed_by", "created_at"),
    ),
    RichContentFieldSpec(
        ChallengeSubmission.__table__,
        "hints",
        kind="hint_list",
        field_name="제출 문제 힌트",
        max_visible_chars=2000,
        guard_columns=("submitter_id", "status", "reviewed_by", "created_at"),
    ),
    RichContentFieldSpec(
        TeachingClass.__table__,
        "description_markdown",
        field_name="강의 설명",
        allow_empty=True,
        max_visible_chars=20000,
        guard_columns=(
            "instructor_id", "status", "created_by", "updated_by",
            "created_at", "updated_at",
        ),
    ),
    RichContentFieldSpec(
        EducationAssignment.__table__,
        "instructions_markdown",
        field_name="과제 안내",
        max_visible_chars=20000,
        guard_columns=(
            "class_id", "status", "created_by", "updated_by",
            "created_at", "updated_at",
        ),
    ),
    RichContentFieldSpec(
        AssignmentSubmission.__table__,
        "content_markdown",
        field_name="과제 제출 본문",
        max_visible_chars=30000,
        guard_columns=("assignment_id", "student_id", "submitted_at", "updated_at"),
    ),
    RichContentFieldSpec(
        AssignmentGrade.__table__,
        "feedback_markdown",
        field_name="채점 피드백",
        allow_empty=True,
        max_visible_chars=12000,
        nullable=True,
        guard_columns=("submission_id", "status", "graded_by", "graded_at", "published_at", "updated_at"),
    ),
    RichContentFieldSpec(
        PlatformEvent.__table__,
        "content_markdown",
        field_name="이벤트 본문",
        max_visible_chars=12000,
        guard_columns=("status", "created_by", "updated_by", "created_at", "updated_at"),
    ),
)


if __name__ == "__main__":
    run_cli(
        service="learn",
        environment=settings.environment,
        engine=engine,
        specs=SPECS,
    )
