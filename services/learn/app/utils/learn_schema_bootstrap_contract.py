"""Pure preflight contract for the explicit Learn schema bootstrap job.

The legacy ``20260813_000`` revision deliberately has no core DDL.  The
container migration job therefore needs one unambiguous path for a completely
empty Learn database, while never treating a partially-created schema as a new
database.  This module has no SQLAlchemy/Alembic dependency so its state
classification is unit-testable without a running database.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum


CORE_TABLES = frozenset({"courses", "lessons", "challenges"})
EDUCATION_TABLES = frozenset(
    {
        "education_role_grants",
        "teaching_classes",
        "teaching_class_enrollments",
        "education_assignments",
        "education_resource_links",
        "education_assignment_submissions",
        "education_assignment_grades",
        "education_assets",
        "education_audit_events",
    }
)
ALEMBIC_VERSION_TABLE = "alembic_version"
# This is the exact table set observed for the pre-Alembic Learn deployment.
# It is intentionally narrow: an operator may opt into adoption only when the
# database is this known legacy shape, never for an arbitrary unversioned DB.
LEGACY_TABLES = frozenset(
    {
        "challenge_knowledge_tags",
        "challenge_learning_prerequisites",
        "challenge_prerequisites",
        "challenge_recommended_prerequisites",
        "challenge_submissions",
        "challenges",
        "course_knowledge_tags",
        "course_prerequisites",
        "courses",
        "flags",
        "hint_unlocks",
        "hints",
        "knowledge_tags",
        "lesson_knowledge_tags",
        "lessons",
        "submissions",
        "user_progress",
    }
)


class LearnSchemaPreparation(StrEnum):
    BOOTSTRAP_EMPTY = "bootstrap_empty"
    ADOPT_LEGACY = "adopt_legacy"
    UPGRADE_EXISTING = "upgrade_existing"


def classify_learn_schema_tables(
    *,
    table_names: Collection[str],
    application_table_names: Collection[str],
    allow_legacy_adoption: bool = False,
) -> LearnSchemaPreparation:
    """Classify a pre-migration database without changing it.

    Only a database with no user tables at all may take the explicit
    ``Base.metadata.create_all`` bootstrap path.  An existing database must
    have an Alembic version marker before it can continue through upgrade;
    a non-empty unversioned legacy database is not adopted automatically.  A
    separately approved operator run may opt in, but only for the exact
    ``LEGACY_TABLES`` shape recorded above.
    A partial core or partial education group is ambiguous and must be
    restored or repaired deliberately rather than guessed at by deployment
    automation.
    """
    actual = frozenset(table_names)
    expected = frozenset(application_table_names)
    if not actual:
        return LearnSchemaPreparation.BOOTSTRAP_EMPTY

    unexpected = actual - expected - {ALEMBIC_VERSION_TABLE}
    if unexpected:
        raise RuntimeError(
            "Learn schema bootstrap은 알 수 없는 사용자 테이블이 있는 DB를 초기화하지 않습니다: "
            f"{', '.join(sorted(unexpected))}."
        )

    application_present = actual.intersection(expected)
    if not application_present:
        raise RuntimeError(
            "Learn schema bootstrap은 alembic 상태만 남은 DB를 빈 DB로 취급하지 않습니다. "
            "검증된 백업 또는 수동 복구 절차를 확인하세요."
        )

    if ALEMBIC_VERSION_TABLE not in actual:
        if allow_legacy_adoption and actual == LEGACY_TABLES:
            return LearnSchemaPreparation.ADOPT_LEGACY
        if allow_legacy_adoption:
            raise RuntimeError(
                "Learn legacy adoption은 승인된 레거시 table 집합에서만 허용됩니다. "
                f"현재 집합과 기대 집합이 다릅니다: {', '.join(sorted(actual ^ LEGACY_TABLES))}."
            )
        raise RuntimeError(
            "Learn schema bootstrap은 version marker가 없는 기존 DB를 자동 채택하지 않습니다. "
            "검증된 legacy adoption 또는 백업 복구 절차를 별도로 수행하세요."
        )

    core_present = actual.intersection(CORE_TABLES)
    if core_present != CORE_TABLES:
        raise RuntimeError(
            "Learn core schema가 부분 생성 상태입니다: "
            f"현재 {', '.join(sorted(core_present)) or '없음'}, "
            f"필수 {', '.join(sorted(CORE_TABLES))}."
        )

    education_present = actual.intersection(EDUCATION_TABLES)
    if education_present and education_present != EDUCATION_TABLES:
        raise RuntimeError(
            "교육 운영 schema가 부분 생성 상태입니다: "
            f"{', '.join(sorted(education_present))}."
        )

    return LearnSchemaPreparation.UPGRADE_EXISTING
