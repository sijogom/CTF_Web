"""강의·과제 교육 운영 V1을 additive하게 추가한다.

기존 Course/Lesson, 지식 그래프, 워게임/CTF 점수·제출 테이블은 변경하지 않는다.
``professor_admin``은 Auth 서비스의 전역 role이 아니라 Learn DB의 교육 전용 grant다.
모든 테이블이 이미 있는 개발 DB는 idempotent하게 통과하지만, 부분 생성 상태는
자동 추측·복구하지 않고 fail-closed로 중단한다.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_010"
down_revision: Union[str, None] = "20260813_009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "education_role_grants",
    "teaching_classes",
    "teaching_class_enrollments",
    "education_assignments",
    "education_resource_links",
    "education_assignment_submissions",
    "education_assignment_grades",
    "education_assets",
    "education_audit_events",
)
_LEGACY_BOOTSTRAP_CORE_TABLES = {"courses", "lessons", "challenges"}

# ``20260813_000`` is intentionally only a legacy baseline.  A table-name
# check alone is therefore not sufficient here: Alembic can otherwise stamp
# this revision on a database where an interrupted/manual DDL operation left a
# table with the right name but without the constraints that keep the education
# domain isolated.  Keep this contract local to the migration so applying a
# revision never depends on importing the running application model.
_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "education_role_grants": frozenset(
        {
            "id", "subject_id", "role", "granted_by", "grant_reason", "granted_at",
            "revoked_by", "revoke_reason", "revoked_at",
        }
    ),
    "teaching_classes": frozenset(
        {
            "id", "title", "summary", "description_markdown", "instructor_id", "status",
            "starts_at", "ends_at", "published_at", "archived_at", "archived_by",
            "archive_reason", "created_by", "updated_by", "created_at", "updated_at",
        }
    ),
    "teaching_class_enrollments": frozenset(
        {
            "id", "class_id", "student_id", "status", "enrolled_by", "enrolled_at",
            "dropped_by", "dropped_at",
        }
    ),
    "education_assignments": frozenset(
        {
            "id", "class_id", "title", "instructions_markdown", "status", "max_score",
            "available_from_at", "due_at", "published_at", "archived_at", "archived_by",
            "archive_reason", "created_by", "updated_by", "created_at", "updated_at",
        }
    ),
    "education_resource_links": frozenset(
        {
            "id", "class_id", "resource_type", "course_id", "lesson_id", "challenge_id",
            "label", "created_by", "created_at",
        }
    ),
    "education_assignment_submissions": frozenset(
        {"id", "assignment_id", "student_id", "content_markdown", "submitted_at", "updated_at"}
    ),
    "education_assignment_grades": frozenset(
        {
            "id", "submission_id", "score", "feedback_markdown", "status", "graded_by",
            "graded_at", "published_at", "updated_at",
        }
    ),
    "education_assets": frozenset(
        {
            "id", "class_id", "assignment_id", "scope", "object_path", "stored_name",
            "original_name", "content_type", "byte_size", "uploaded_by", "created_at",
            "deleted_by", "deleted_at", "delete_reason",
        }
    ),
    "education_audit_events": frozenset(
        {"id", "actor_id", "action", "entity_type", "entity_id", "class_id", "details", "created_at"}
    ),
}
_REQUIRED_INDEXES: dict[str, frozenset[str]] = {
    "education_role_grants": frozenset(
        {
            "ix_education_role_grants_subject_id", "ix_education_role_grants_granted_by",
            "ix_education_role_grants_granted_at", "ix_education_role_grants_revoked_by",
            "ix_education_role_grants_revoked_at", "uq_education_role_grants_active_subject",
            "ix_education_role_grants_subject_active",
        }
    ),
    "teaching_classes": frozenset(
        {
            "ix_teaching_classes_instructor_id", "ix_teaching_classes_status",
            "ix_teaching_classes_starts_at", "ix_teaching_classes_ends_at",
            "ix_teaching_classes_archived_by", "ix_teaching_classes_created_by",
            "ix_teaching_classes_updated_by", "ix_teaching_classes_status_starts_at",
            "ix_teaching_classes_instructor_status",
        }
    ),
    "teaching_class_enrollments": frozenset(
        {
            "ix_teaching_class_enrollments_class_id", "ix_teaching_class_enrollments_student_id",
            "ix_teaching_class_enrollments_status", "ix_teaching_class_enrollments_enrolled_by",
            "ix_teaching_class_enrollments_enrolled_at", "ix_teaching_class_enrollments_dropped_by",
            "ix_teaching_class_enrollments_student_status", "ix_teaching_class_enrollments_class_status",
        }
    ),
    "education_assignments": frozenset(
        {
            "ix_education_assignments_class_id", "ix_education_assignments_status",
            "ix_education_assignments_available_from_at", "ix_education_assignments_due_at",
            "ix_education_assignments_archived_by", "ix_education_assignments_created_by",
            "ix_education_assignments_updated_by", "ix_education_assignments_class_status",
            "ix_education_assignments_class_due_at",
        }
    ),
    "education_resource_links": frozenset(
        {
            "ix_education_resource_links_class_id", "ix_education_resource_links_resource_type",
            "ix_education_resource_links_course_id", "ix_education_resource_links_lesson_id",
            "ix_education_resource_links_challenge_id", "ix_education_resource_links_created_by",
            "uq_education_resource_links_course", "uq_education_resource_links_lesson",
            "uq_education_resource_links_wargame", "ix_education_resource_links_class_created",
        }
    ),
    "education_assignment_submissions": frozenset(
        {
            "ix_education_assignment_submissions_assignment_id",
            "ix_education_assignment_submissions_student_id",
            "ix_education_assignment_submissions_submitted_at",
            "ix_education_assignment_submissions_updated_at",
            "ix_education_assignment_submissions_assignment_submitted",
            "ix_education_assignment_submissions_student_updated",
        }
    ),
    "education_assignment_grades": frozenset(
        {
            "ix_education_assignment_grades_submission_id", "ix_education_assignment_grades_status",
            "ix_education_assignment_grades_graded_by", "ix_education_assignment_grades_status_published",
        }
    ),
    "education_assets": frozenset(
        {
            "ix_education_assets_class_id", "ix_education_assets_assignment_id",
            "ix_education_assets_scope", "ix_education_assets_uploaded_by",
            "ix_education_assets_deleted_by", "ix_education_assets_deleted_at",
            "ix_education_assets_class_active", "ix_education_assets_assignment_active",
        }
    ),
    "education_audit_events": frozenset(
        {
            "ix_education_audit_events_actor_id", "ix_education_audit_events_action",
            "ix_education_audit_events_entity_type", "ix_education_audit_events_entity_id",
            "ix_education_audit_events_class_id", "ix_education_audit_events_created_at",
            "ix_education_audit_events_class_created", "ix_education_audit_events_actor_created",
        }
    ),
}
_REQUIRED_UNIQUE_COLUMNS: dict[str, frozenset[frozenset[str]]] = {
    "teaching_class_enrollments": frozenset({frozenset({"class_id", "student_id"})}),
    "education_assignment_submissions": frozenset({frozenset({"assignment_id", "student_id"})}),
    "education_assignment_grades": frozenset({frozenset({"submission_id"})}),
    "education_assets": frozenset({frozenset({"object_path"})}),
}
_REQUIRED_FOREIGN_KEYS: dict[str, frozenset[tuple[str, str]]] = {
    "teaching_class_enrollments": frozenset({("class_id", "teaching_classes")}),
    "education_assignments": frozenset({("class_id", "teaching_classes")}),
    "education_resource_links": frozenset(
        {
            ("class_id", "teaching_classes"), ("course_id", "courses"),
            ("lesson_id", "lessons"), ("challenge_id", "challenges"),
        }
    ),
    "education_assignment_submissions": frozenset({("assignment_id", "education_assignments")}),
    "education_assignment_grades": frozenset({("submission_id", "education_assignment_submissions")}),
    "education_assets": frozenset(
        {("class_id", "teaching_classes"), ("assignment_id", "education_assignments")}
    ),
    "education_audit_events": frozenset({("class_id", "teaching_classes")}),
}
_REQUIRED_CHECKS: dict[str, frozenset[str]] = {
    "education_role_grants": frozenset(
        {"ck_education_role_grants_role", "ck_education_role_grants_revoke_after_grant"}
    ),
    "teaching_classes": frozenset({"ck_teaching_classes_status", "ck_teaching_classes_schedule"}),
    "teaching_class_enrollments": frozenset(
        {"ck_teaching_class_enrollments_status", "ck_teaching_class_enrollments_drop_state"}
    ),
    "education_assignments": frozenset(
        {
            "ck_education_assignments_status", "ck_education_assignments_max_score",
            "ck_education_assignments_submission_window",
        }
    ),
    "education_resource_links": frozenset(
        {"ck_education_resource_links_type", "ck_education_resource_links_exact_target"}
    ),
    "education_assignment_grades": frozenset(
        {"ck_education_assignment_grades_score_nonnegative", "ck_education_assignment_grades_status"}
    ),
    "education_assets": frozenset(
        {
            "ck_education_assets_scope", "ck_education_assets_scope_target",
            "ck_education_assets_positive_size", "ck_education_assets_delete_after_create",
        }
    ),
    "education_audit_events": frozenset({"ck_education_audit_events_entity_type"}),
}


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _schema_contract_missing(inspector) -> list[str]:
    """Return safety-critical differences without trying to repair them.

    This migration must never convert a manually/partially-created education
    schema into a stamped head revision.  Missing performance indexes are
    included because the permission, quota, and audit queries rely on them at
    normal operating scale; missing constraints are included because they are
    data-integrity boundaries.
    """
    missing: list[str] = []
    for table, required_columns in _REQUIRED_COLUMNS.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table)}
        for column in sorted(required_columns - actual_columns):
            missing.append(f"{table}.column:{column}")

        actual_indexes = {index["name"] for index in inspector.get_indexes(table) if index.get("name")}
        for index in sorted(_REQUIRED_INDEXES[table] - actual_indexes):
            missing.append(f"{table}.index:{index}")

        actual_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table)
            if constraint.get("name")
        }
        for check in sorted(_REQUIRED_CHECKS.get(table, frozenset()) - actual_checks):
            missing.append(f"{table}.check:{check}")

        primary_key = set((inspector.get_pk_constraint(table) or {}).get("constrained_columns") or [])
        if primary_key != {"id"}:
            missing.append(f"{table}.primary_key:id")

        actual_unique_columns = {
            frozenset(constraint.get("column_names") or [])
            for constraint in inspector.get_unique_constraints(table)
            if constraint.get("column_names")
        }
        for required_unique in _REQUIRED_UNIQUE_COLUMNS.get(table, frozenset()):
            if required_unique not in actual_unique_columns:
                missing.append(f"{table}.unique:{','.join(sorted(required_unique))}")

        actual_foreign_keys = {
            (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
            for foreign_key in inspector.get_foreign_keys(table)
            if len(foreign_key.get("constrained_columns") or []) == 1
            and foreign_key.get("referred_table")
        }
        for required_foreign_key in _REQUIRED_FOREIGN_KEYS.get(table, frozenset()):
            if required_foreign_key not in actual_foreign_keys:
                missing.append(
                    f"{table}.foreign_key:{required_foreign_key[0]}->{required_foreign_key[1]}"
                )
    return missing


def _assert_complete_or_absent(*, action: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names()).intersection(_TABLES)
    if not present:
        return False
    if present == set(_TABLES):
        missing = _schema_contract_missing(inspector)
        if not missing:
            return True
        preview = ", ".join(missing[:8])
        raise RuntimeError(
            f"교육 운영 {action}의 테이블은 모두 있으나 스키마 계약이 불완전합니다: {preview}. "
            "자동 보정·revision stamp를 중단하고 검증된 백업과 수동 복구 계획을 확인하세요."
        )
    raise RuntimeError(
        f"교육 운영 {action}의 테이블 상태가 불완전합니다. "
        "자동 추측을 중단하고 검증된 백업과 스키마 상태를 확인하세요."
    )


def _create_common_indexes() -> None:
    # education_role_grants
    op.create_index("ix_education_role_grants_subject_id", "education_role_grants", ["subject_id"])
    op.create_index("ix_education_role_grants_granted_by", "education_role_grants", ["granted_by"])
    op.create_index("ix_education_role_grants_granted_at", "education_role_grants", ["granted_at"])
    op.create_index("ix_education_role_grants_revoked_by", "education_role_grants", ["revoked_by"])
    op.create_index("ix_education_role_grants_revoked_at", "education_role_grants", ["revoked_at"])
    op.create_index(
        "uq_education_role_grants_active_subject",
        "education_role_grants",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_education_role_grants_subject_active",
        "education_role_grants",
        ["subject_id", "revoked_at"],
    )

    # teaching_classes
    for column in ("instructor_id", "status", "starts_at", "ends_at", "archived_by", "created_by", "updated_by"):
        op.create_index(f"ix_teaching_classes_{column}", "teaching_classes", [column])
    op.create_index("ix_teaching_classes_status_starts_at", "teaching_classes", ["status", "starts_at"])
    op.create_index("ix_teaching_classes_instructor_status", "teaching_classes", ["instructor_id", "status"])

    # teaching_class_enrollments
    for column in ("class_id", "student_id", "status", "enrolled_by", "enrolled_at", "dropped_by"):
        op.create_index(f"ix_teaching_class_enrollments_{column}", "teaching_class_enrollments", [column])
    op.create_index(
        "ix_teaching_class_enrollments_student_status",
        "teaching_class_enrollments",
        ["student_id", "status"],
    )
    op.create_index(
        "ix_teaching_class_enrollments_class_status",
        "teaching_class_enrollments",
        ["class_id", "status"],
    )

    # education_assignments
    for column in ("class_id", "status", "available_from_at", "due_at", "archived_by", "created_by", "updated_by"):
        op.create_index(f"ix_education_assignments_{column}", "education_assignments", [column])
    op.create_index("ix_education_assignments_class_status", "education_assignments", ["class_id", "status"])
    op.create_index("ix_education_assignments_class_due_at", "education_assignments", ["class_id", "due_at"])

    # education_resource_links
    for column in ("class_id", "resource_type", "course_id", "lesson_id", "challenge_id", "created_by"):
        op.create_index(f"ix_education_resource_links_{column}", "education_resource_links", [column])
    op.create_index(
        "uq_education_resource_links_course",
        "education_resource_links",
        ["class_id", "course_id"],
        unique=True,
        postgresql_where=sa.text("resource_type = 'course'"),
    )
    op.create_index(
        "uq_education_resource_links_lesson",
        "education_resource_links",
        ["class_id", "lesson_id"],
        unique=True,
        postgresql_where=sa.text("resource_type = 'lesson'"),
    )
    op.create_index(
        "uq_education_resource_links_wargame",
        "education_resource_links",
        ["class_id", "challenge_id"],
        unique=True,
        postgresql_where=sa.text("resource_type = 'wargame'"),
    )
    op.create_index(
        "ix_education_resource_links_class_created",
        "education_resource_links",
        ["class_id", "created_at"],
    )

    # submissions and grades
    for column in ("assignment_id", "student_id", "submitted_at", "updated_at"):
        op.create_index(f"ix_education_assignment_submissions_{column}", "education_assignment_submissions", [column])
    op.create_index(
        "ix_education_assignment_submissions_assignment_submitted",
        "education_assignment_submissions",
        ["assignment_id", "submitted_at"],
    )
    op.create_index(
        "ix_education_assignment_submissions_student_updated",
        "education_assignment_submissions",
        ["student_id", "updated_at"],
    )
    for column in ("submission_id", "status", "graded_by"):
        op.create_index(f"ix_education_assignment_grades_{column}", "education_assignment_grades", [column])
    op.create_index(
        "ix_education_assignment_grades_status_published",
        "education_assignment_grades",
        ["status", "published_at"],
    )

    # private assets
    for column in ("class_id", "assignment_id", "scope", "uploaded_by", "deleted_by", "deleted_at"):
        op.create_index(f"ix_education_assets_{column}", "education_assets", [column])
    op.create_index("ix_education_assets_class_active", "education_assets", ["class_id", "deleted_at"])
    op.create_index("ix_education_assets_assignment_active", "education_assets", ["assignment_id", "deleted_at"])

    # append-only audit events
    for column in ("actor_id", "action", "entity_type", "entity_id", "class_id", "created_at"):
        op.create_index(f"ix_education_audit_events_{column}", "education_audit_events", [column])
    op.create_index("ix_education_audit_events_class_created", "education_audit_events", ["class_id", "created_at"])
    op.create_index("ix_education_audit_events_actor_created", "education_audit_events", ["actor_id", "created_at"])


def upgrade() -> None:
    if _assert_complete_or_absent(action="upgrade"):
        return
    # 현재 legacy 000 baseline은 기존 core ORM 테이블을 Alembic으로 만들지 않는다.
    # 깨끗한 DB는 application worker가 아니라 app.scripts.learn_migrate의 명시
    # bootstrap(create_all -> schema verify -> stamp)만 처리한다. 이 revision을
    # 직접 실행할 때 core가 없으면 FK 실패 후 일부 education table이 남을 수 있으므로,
    # 이 단계에서는 절대 partial create를 시작하지 않는다.
    missing_core = _LEGACY_BOOTSTRAP_CORE_TABLES - _table_names()
    if missing_core:
        raise RuntimeError(
            "교육 migration은 legacy core schema가 없는 깨끗한 DB에 revision을 stamp하지 않습니다. "
            f"누락 core table: {', '.join(sorted(missing_core))}. "
            "명시 Learn bootstrap 또는 검증된 core schema migration을 적용한 뒤 다시 실행하세요."
        )

    op.create_table(
        "education_role_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default=sa.text("'professor_admin'")),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_reason", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role = 'professor_admin'", name="ck_education_role_grants_role"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_education_role_grants_revoke_after_grant",
        ),
    )

    op.create_table(
        "teaching_classes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("description_markdown", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("instructor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_teaching_classes_status"),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_teaching_classes_schedule",
        ),
    )

    op.create_table(
        "teaching_class_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'active'")),
        sa.Column("enrolled_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("dropped_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("class_id", "student_id", name="uq_teaching_class_enrollments_student"),
        sa.CheckConstraint("status IN ('active','dropped')", name="ck_teaching_class_enrollments_status"),
        sa.CheckConstraint(
            "(status = 'active' AND dropped_at IS NULL) OR "
            "(status = 'dropped' AND dropped_at IS NOT NULL)",
            name="ck_teaching_class_enrollments_drop_state",
        ),
    )

    op.create_table(
        "education_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("instructions_markdown", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("available_from_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_education_assignments_status"),
        sa.CheckConstraint("max_score BETWEEN 1 AND 1000", name="ck_education_assignments_max_score"),
        sa.CheckConstraint(
            "due_at IS NULL OR available_from_at IS NULL OR due_at > available_from_at",
            name="ck_education_assignments_submission_window",
        ),
    )

    op.create_table(
        "education_resource_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("courses.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("lesson_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("lessons.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("challenge_id", sa.Integer(), sa.ForeignKey("challenges.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("resource_type IN ('course','lesson','wargame')", name="ck_education_resource_links_type"),
        sa.CheckConstraint(
            "(resource_type = 'course' AND course_id IS NOT NULL AND lesson_id IS NULL AND challenge_id IS NULL) OR "
            "(resource_type = 'lesson' AND course_id IS NULL AND lesson_id IS NOT NULL AND challenge_id IS NULL) OR "
            "(resource_type = 'wargame' AND course_id IS NULL AND lesson_id IS NULL AND challenge_id IS NOT NULL)",
            name="ck_education_resource_links_exact_target",
        ),
    )

    op.create_table(
        "education_assignment_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("education_assignments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_markdown", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("assignment_id", "student_id", name="uq_education_assignment_submissions_student"),
    )

    op.create_table(
        "education_assignment_grades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("education_assignment_submissions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("feedback_markdown", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("graded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("submission_id", name="uq_education_assignment_grades_submission"),
        sa.CheckConstraint("score >= 0", name="ck_education_assignment_grades_score_nonnegative"),
        sa.CheckConstraint("status IN ('draft','published')", name="ck_education_assignment_grades_status"),
    )

    op.create_table(
        "education_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("education_assignments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("object_path", sa.String(length=1024), nullable=False, unique=True),
        sa.Column("stored_name", sa.String(length=180), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("scope IN ('class','assignment')", name="ck_education_assets_scope"),
        sa.CheckConstraint(
            "(scope = 'class' AND assignment_id IS NULL) OR "
            "(scope = 'assignment' AND assignment_id IS NOT NULL)",
            name="ck_education_assets_scope_target",
        ),
        sa.CheckConstraint("byte_size > 0", name="ck_education_assets_positive_size"),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="ck_education_assets_delete_after_create",
        ),
    )

    op.create_table(
        "education_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teaching_classes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "entity_type IN ('role_grant','class','enrollment','assignment','resource_link','submission','grade','asset')",
            name="ck_education_audit_events_entity_type",
        ),
    )
    _create_common_indexes()


def downgrade() -> None:
    if not _assert_complete_or_absent(action="downgrade"):
        return
    bind = op.get_bind()
    counts = {
        table: int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in _TABLES
    }
    if any(counts.values()):
        raise RuntimeError(
            "교육 운영 데이터 또는 감사 기록이 존재하므로 destructive downgrade를 중단합니다. "
            "검증된 백업·보존 계획 없이 강의·제출·성적·감사 데이터를 삭제하지 마세요."
        )

    # FK 역순이다. 인덱스는 PostgreSQL에서 table drop과 함께 제거된다.
    op.drop_table("education_audit_events")
    op.drop_table("education_assignment_grades")
    op.drop_table("education_assignment_submissions")
    op.drop_table("education_assets")
    op.drop_table("education_resource_links")
    op.drop_table("education_assignments")
    op.drop_table("teaching_class_enrollments")
    op.drop_table("teaching_classes")
    op.drop_table("education_role_grants")
